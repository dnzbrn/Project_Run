#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import logging
import re
import hmac
import hashlib
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO
import base64
import json

from flask import Flask, request, render_template,render_template_string, redirect, url_for, session, jsonify, abort
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker
from openai import OpenAI
import requests
from flask_mail import Mail, Message

# ================================================
# CONFIGURAÇÃO INICIAL
# ================================================

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('treinorun.log')
    ]
)

basedir = os.path.abspath(os.path.dirname(__file__))
template_dir = os.path.join(basedir, 'Templates')

app = Flask(__name__, template_folder=template_dir)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24).hex())

# Configurações de segurança
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    JSONIFY_PRETTYPRINT_REGULAR=False,
    TRAP_HTTP_EXCEPTIONS=True
)

# ProxyFix para Railway
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

# Rate Limiter
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri=os.getenv("REDIS_URL", "memory://"),
    strategy="moving-window",
    default_limits=["200 per day", "50 per hour"]
)

# ================================================
# BANCO DE DADOS E SERVIÇOS
# ================================================

engine = create_engine(
    os.getenv("DATABASE_URL"),
    pool_size=10,
    max_overflow=0,
    pool_recycle=3600,
    pool_pre_ping=True
)
db = scoped_session(sessionmaker(bind=engine))

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=30.0
)

# Configuração de e-mail
app.config['MAIL_SERVER'] = 'smtp.zoho.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('ZOHO_EMAIL')
app.config['MAIL_PASSWORD'] = os.getenv('ZOHO_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = ('TreinoRun', os.getenv('ZOHO_EMAIL'))
mail = Mail(app)

# ================================================
# DECORATOR PARA ROTAS ASSÍNCRONAS
# ================================================

def async_route(f):
    """Permite usar async/await em rotas Flask"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(f(*args, **kwargs))
        finally:
            loop.close()
    return wrapper

# ================================================
# FUNÇÕES AUXILIARES
# ================================================

def validar_assinatura(body, signature):
    chave_secreta = os.getenv("MERCADO_PAGO_WEBHOOK_SECRET").encode("utf-8")
    assinatura_esperada = hmac.new(chave_secreta, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(assinatura_esperada, signature)

def pode_gerar_plano(email, plano):
    try:
        usuario = db.execute(
            text("SELECT * FROM usuarios WHERE email = :email"),
            {"email": email}
        ).fetchone()

        if plano == "anual":
            if usuario:
                assinatura = db.execute(
                    text("SELECT status FROM assinaturas WHERE usuario_id = :usuario_id ORDER BY id DESC LIMIT 1"),
                    {"usuario_id": usuario.id}
                ).fetchone()
                return assinatura and assinatura.status == "active"
            return False

        elif plano == "gratuito":
            if usuario and usuario.ultima_geracao:
                ultima_geracao = datetime.strptime(str(usuario.ultima_geracao), "%Y-%m-%d %H:%M:%S")
                return (datetime.now() - ultima_geracao).days >= 30
            return True

        return False
    except Exception as e:
        logging.error(f"Erro ao verificar permissão de plano: {e}")
        return False

def calcular_semanas(tempo_melhoria):
    try:
        tempo_melhoria = tempo_melhoria.lower()
        match = re.search(r'(\d+)\s*(semanas?|meses?|mês)', tempo_melhoria)
        
        if not match:
            return 4
        
        valor = int(match.group(1))
        unidade = match.group(2)
        
        if 'semana' in unidade:
            return min(valor, 52)
        elif 'mes' in unidade or 'mês' in unidade:
            return min(valor * 4, 52)
        else:
            return 4
    except Exception as e:
        logging.error(f"Erro ao calcular semanas: {e}")
        return 4

def registrar_geracao(email, plano):
    try:
        hoje = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        usuario = db.execute(
            text("SELECT * FROM usuarios WHERE email = :email"),
            {"email": email}
        ).fetchone()

        if not usuario:
            db.execute(
                text("""
                    INSERT INTO usuarios (email, plano, data_inscricao, ultima_geracao)
                    VALUES (:email, :plano, :data_inscricao, :ultima_geracao)
                    RETURNING id
                """),
                {"email": email, "plano": plano, "data_inscricao": hoje, "ultima_geracao": hoje},
            )
            usuario_id = db.fetchone()[0]
        else:
            db.execute(
                text("UPDATE usuarios SET ultima_geracao = :ultima_geracao, plano = :plano WHERE email = :email"),
                {"ultima_geracao": hoje, "email": email, "plano": plano},
            )
            usuario_id = usuario.id

        db.execute(
            text("INSERT INTO geracoes (usuario_id, data_geracao) VALUES (:usuario_id, :data_geracao)"),
            {"usuario_id": usuario_id, "data_geracao": hoje},
        )
        db.commit()
        return True
    except Exception as e:
        logging.error(f"Erro ao registrar geração: {e}")
        db.rollback()
        return False

async def gerar_plano_openai(prompt, semanas):
    try:
        model = "gpt-3.5-turbo-16k" if semanas > 12 else "gpt-3.5-turbo"
        
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Você é um treinador de corrida experiente. Siga exatamente as instruções fornecidas."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Erro ao gerar plano: {e}")
        return "Erro ao gerar o plano. Tente novamente mais tarde."

def enviar_email_confirmacao_pagamento(email, nome="Cliente"):
    try:
        msg = Message(
            subject="✅ Pagamento Confirmado - TreinoRun",
            recipients=[email],
            html=f"""
            <!DOCTYPE html>
            <html>
            <head>
                <style>
                    body {{ font-family: Arial, sans-serif; line-height: 1.6; }}
                    .header {{ background-color: #27ae60; color: white; padding: 20px; text-align: center; }}
                    .content {{ padding: 20px; }}
                    .footer {{ text-align: center; padding: 10px; font-size: 12px; color: #777; }}
                </style>
            </head>
            <body>
                <div class="header">
                    <h2>Pagamento Confirmado!</h2>
                </div>
                <div class="content">
                    <p>Olá {nome},</p>
                    <p>Seu pagamento foi processado com sucesso e seu plano anual foi ativado!</p>
                    <p>Agora você pode gerar quantos planos de treino quiser durante 1 ano.</p>
                    <p><a href="https://treinorun.com.br/seutreino" style="background-color: #27ae60; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Criar Novo Treino</a></p>
                </div>
                <div class="footer">
                    <p>© {datetime.now().year} TreinoRun • <a href="https://treinorun.com.br">www.treinorun.com.br</a></p>
                </div>
            </body>
            </html>
            """
        )
        mail.send(msg)
        logging.info(f"E-mail de confirmação enviado para {email}")
        return True
    except Exception as e:
        logging.error(f"Erro ao enviar e-mail de confirmação: {e}")
        return False

# ================================================
# ROTAS PRINCIPAIS
# ================================================

@app.route("/")
@limiter.limit("100 per hour")
def landing():
    try:
        return render_template("landing.html")
    except Exception as e:
        logging.error(f"Erro ao renderizar landing.html: {e}")
        return render_template_string("""
            <!DOCTYPE html>
            <html>
            <body>
                <h1>TreinoRun - Página Inicial</h1>
                <p>Aplicação está funcionando, mas o template não foi carregado.</p>
                <a href="/seutreino">Criar Treino</a>
            </body>
            </html>
        """), 200

@app.route("/blog")
@limiter.limit("100 per hour")
def blog():
    try:
        return render_template("blog.html")
    except Exception as e:
        logging.error(f"Erro ao renderizar blog.html: {e}")
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Blog - TreinoRun</title>
        </head>
        <body>
            <h1>Blog TreinoRun</h1>
            <p>Conteúdo do blog não pôde ser carregado.</p>
            <a href="/">Voltar para a página inicial</a>
        </body>
        </html>
        """, 200

@app.route("/seutreino")
@limiter.limit("100 per hour")
def seutreino():
    try:
        return render_template("seutreino.html")
    except Exception as e:
        logging.error(f"Erro ao renderizar seutreino.html: {e}")
        return render_template_string("""
            <!DOCTYPE html>
            <html>
            <body>
                <h1>Criar Seu Treino</h1>
                <p>Formulário de criação de treino</p>
            </body>
            </html>
        """), 200

@app.route("/sucesso")
@limiter.limit("100 per hour")
def sucesso():
    try:
        return render_template("sucesso.html")
    except Exception as e:
        logging.error(f"Erro ao renderizar sucesso.html: {e}")
        return render_template_string("""
            <!DOCTYPE html>
            <html>
            <body>
                <h1>Sucesso!</h1>
                <p>Pagamento aprovado e plano ativado com sucesso.</p>
                <a href="/seutreino">Criar seu primeiro treino</a>
            </body>
            </html>
        """), 200

@app.route("/pendente")
@limiter.limit("100 per hour")
def pendente():
    try:
        return render_template("pendente.html")
    except Exception as e:
        logging.error(f"Erro ao renderizar pendente.html: {e}")
        return render_template_string("""
            <!DOCTYPE html>
            <html>
            <body>
                <h1>Pagamento Pendente</h1>
                <p>Seu pagamento está sendo processado.</p>
                <p>Você receberá um e-mail quando for confirmado.</p>
            </body>
            </html>
        """), 200

@app.route("/erro")
@limiter.limit("100 per hour")
def erro():
    try:
        return render_template("erro.html")
    except Exception as e:
        logging.error(f"Erro ao renderizar erro.html: {e}")
        return render_template_string("""
            <!DOCTYPE html>
            <html>
            <body>
                <h1>Erro no Pagamento</h1>
                <p>Ocorreu um problema com seu pagamento.</p>
                <a href="/seutreino">Tentar novamente</a>
            </body>
            </html>
        """), 200

@app.route("/resultado")
@limiter.limit("100 per hour")
def resultado():
    try:
        titulo = session.get("titulo", "Plano de Treino")
        plano = session.get("plano", "Nenhum plano gerado.")
        return render_template("resultado.html", titulo=titulo, plano=plano)
    except Exception as e:
        logging.error(f"Erro ao renderizar resultado.html: {e}")
        return f"""
        <!DOCTYPE html>
        <html>
        <body>
            <h1>{session.get('titulo', 'Plano de Treino')}</h1>
            <pre>{session.get('plano', 'Nenhum plano gerado.')}</pre>
        </body>
        </html>
        """, 200

# ================================================
# ROTAS DE GERAÇÃO DE PLANOS
# ================================================

@app.route("/generate", methods=["POST"])
@limiter.limit("10 per hour")
@async_route
async def generate():
    dados_usuario = request.form
    required_fields = ["email", "plano", "objetivo", "tempo_melhoria", "nivel", "dias", "tempo"]
    
    if not all(field in dados_usuario for field in required_fields):
        return "Dados do formulário incompletos.", 400

    email = dados_usuario["email"]
    plano = dados_usuario["plano"]

    if not pode_gerar_plano(email, plano):
        if plano == "anual":
            return redirect(url_for("iniciar_pagamento", email=email))
        return "Você já gerou um plano gratuito este mês. Atualize para o plano anual para gerar mais planos.", 400

    semanas = calcular_semanas(dados_usuario['tempo_melhoria'])
    
    if "maratona" in dados_usuario['objetivo'].lower() and semanas < 16:
        return "Preparação para maratona requer mínimo de 16 semanas", 400
    elif "meia-maratona" in dados_usuario['objetivo'].lower() and semanas < 12:
        return "Preparação para meia-maratona requer mínimo de 12 semanas", 400

    prompt = f"""
    Crie um plano de corrida detalhado que GARANTA que o usuário atinja: {dados_usuario['objetivo']} 
    em {dados_usuario['tempo_melhoria']}. Siga rigorosamente:

    REQUISITOS:
    1. OBJETIVO FINAL:
       - Na semana {semanas}, o usuário deve conseguir realizar: {dados_usuario['objetivo']}
    
    2. ESTRUTURA DIÁRIOS:
       - Detalhe cada sessão com:
         * Divisão do tempo em blocos
         * Ritmos específicos para cada segmento
         * Exemplo: "30min = 5' aquecimento (6:30/km) + 20' principal (10' a 6:00/km + 10' a 5:45/km) + 5' desaquecimento (7:00/km)"
    
    3. PROGRESSÃO SEMANAL:
       - Mostre claramente como evolui até o objetivo final
       - Exemplo:
         "Semana 1: Corrida contínua a 6:40/km
         Semana 4: Introdução a intervalos (5:50/km)
         Semana {semanas}: Ritmo-alvo para {dados_usuario['objetivo']} (5:20/km)"
    
    4. ADAPTAÇÕES:
       - Nível: {dados_usuario['nivel']}
       - Dias/semana: {dados_usuario['dias']}
       - Tempo/sessão: {dados_usuario['tempo']} minutos

    FORMATO DE RESPOSTA:
    - Semana a semana com descrição completa
    - Ritmos específicos para cada tipo de treino
    - Garantia de que na semana final o objetivo será alcançado
    """

    plano_gerado = await gerar_plano_openai(prompt, semanas)
    if registrar_geracao(email, plano):
        session["titulo"] = f"Plano para: {dados_usuario['objetivo']}"
        session["plano"] = "Este plano é gerado automaticamente. Consulte um profissional para ajustes.\n\n" + plano_gerado
        return redirect(url_for("resultado"))
    else:
        return "Erro ao registrar seu plano. Tente novamente.", 500

# ================================================
# ROTAS DE PAGAMENTO (CORRIGIDAS)
# ================================================

@app.route("/iniciar_pagamento", methods=["GET", "POST"])
@limiter.limit("5 per hour")
def iniciar_pagamento():
    dados_usuario = request.form if request.method == "POST" else request.args

    if "email" not in dados_usuario:
        return "Email não fornecido.", 400

    email = dados_usuario["email"]

    try:
        # Registrar tentativa de pagamento
        db.execute(
            text("""
                INSERT INTO tentativas_pagamento (email, data_tentativa) 
                VALUES (:email, NOW())
            """),
            {"email": email}
        )
        db.commit()
    except Exception as e:
        logging.error(f"Erro ao registrar tentativa de pagamento: {e}")
        db.rollback()

    # Criar preferência de pagamento no Mercado Pago
    payload = {
        "items": [{
            "title": "Plano Anual de Treino - TreinoRun",
            "description": "Acesso ilimitado por 1 ano à geração de planos de treino personalizados",
            "quantity": 1,
            "unit_price": 59.9,
            "currency_id": "BRL",
        }],
        "payer": {
            "email": email,
            "name": dados_usuario.get("nome", "Cliente")
        },
        "back_urls": {
            "success": url_for("sucesso", _external=True),
            "failure": url_for("erro", _external=True),
            "pending": url_for("pendente", _external=True)
        },
        "auto_return": "approved",
        "notification_url": url_for("mercadopago_webhook", _external=True),
        "external_reference": email,
        "statement_descriptor": "TREINORUN"
    }

    try:
        response = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers={
                "Authorization": f"Bearer {os.getenv('MERCADO_PAGO_ACCESS_TOKEN')}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10
        )
        response.raise_for_status()
        
        # Log da resposta do Mercado Pago
        logging.info(f"Resposta do Mercado Pago: {response.json()}")
        
        return redirect(response.json()["init_point"])
    except requests.exceptions.RequestException as e:
        logging.error(f"Erro ao criar preferência de pagamento: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Resposta do Mercado Pago: {e.response.text}")
        return "Erro ao processar o pagamento. Tente novamente mais tarde.", 500

@app.route("/webhook/mercadopago", methods=["POST"])
@limiter.limit("100 per day")
def mercadopago_webhook():
    try:
        # 1. Registro inicial no log
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        logging.info(f"Webhook recebido - IP: {client_ip}")
        
        # 2. Verificar se é uma notificação de teste
        dados_brutos = request.data.decode('utf-8')
        if dados_brutos == 'TEST_NOTIFICATION':
            logging.info("Notificação de teste recebida")
            registrar_log(
                payload=dados_brutos,
                status_processamento='teste',
                mensagem_erro=None
            )
            return jsonify({"status": "teste_recebido"}), 200
        
        # 3. Processar o payload JSON
        try:
            payload = request.get_json()
            if not payload:
                raise ValueError("Payload vazio")
                
            # Normalizar o payload para diferentes formatos de notificação
            payload = normalizar_payload(payload)
            
        except Exception as e:
            erro_msg = f"Payload JSON inválido: {str(e)}"
            logging.error(erro_msg)
            registrar_log(
                payload=dados_brutos,
                status_processamento='erro',
                mensagem_erro=erro_msg
            )
            return jsonify({"erro": "JSON inválido"}), 400

        # 4. Registrar a notificação recebida
        registrar_log(
            payload=json.dumps(payload),
            status_processamento='recebido',
            mensagem_erro=None
        )

        # 5. Processar conforme o tipo de notificação
        tipo = payload.get("type")
        
        if tipo == "subscription_preapproval" or tipo == "preapproval":
            return processar_assinatura(payload)
        elif tipo == "payment":
            return processar_pagamento(payload)
        else:
            msg = f"Tipo de notificação não tratado: {tipo}"
            logging.info(msg)
            registrar_log(
                payload=json.dumps(payload),
                status_processamento='ignorado',
                mensagem_erro=msg
            )
            return jsonify({"status": "ignorado"}), 200
            
    except Exception as e:
        erro_msg = f"Erro fatal no webhook: {str(e)}"
        logging.error(erro_msg, exc_info=True)
        registrar_log(
            payload=dados_brutos if 'dados_brutos' in locals() else 'Não disponível',
            status_processamento='erro_fatal',
            mensagem_erro=erro_msg
        )
        return jsonify({"erro": "Erro interno no servidor"}), 500

def normalizar_payload(payload_original):
    """
    Normaliza diferentes formatos de notificação do Mercado Pago para um formato padrão
    """
    payload = payload_original.copy()
    
    # Caso 1: Notificação de atualização de pagamento (payment.updated)
    if payload.get("action") in ["payment.updated", "updated"] and "data" in payload:
        if payload.get("type") == "payment" or payload.get("entity") == "payment":
            return {
                "data": {"id": payload["data"]["id"]},
                "type": "payment"
            }
    
    # Caso 2: Notificação de assinatura (subscription_preapproval)
    if payload.get("entity") == "preapproval" or payload.get("type") == "subscription_preapproval":
        return {
            "data": {"id": payload["data"]["id"]},
            "type": "subscription_preapproval"
        }
    
    # Caso 3: Notificação padrão (já no formato correto)
    return payload

def registrar_log(payload, status_processamento, mensagem_erro=None):
    """Registra a tentativa no banco de dados"""
    try:
        db.execute(
            text("""
                INSERT INTO logs_webhook (
                    data_recebimento,
                    payload,
                    status_processamento,
                    mensagem_erro
                ) VALUES (
                    NOW(),
                    :payload,
                    :status_processamento,
                    :mensagem_erro
                )
            """),
            {
                "payload": payload,
                "status_processamento": status_processamento,
                "mensagem_erro": mensagem_erro
            }
        )
        db.commit()
    except Exception as e:
        logging.error(f"Falha ao registrar log no BD: {str(e)}")
        db.rollback()

def processar_assinatura(payload):
    try:
        subscription_id = payload["data"]["id"]
        logging.info(f"Processando assinatura: {subscription_id}")
        
        # 1. Obter detalhes da assinatura da API do Mercado Pago
        detalhes_assinatura = obter_detalhes_assinatura(subscription_id)
        
        if not detalhes_assinatura:
            raise ValueError("Não foi possível obter detalhes da assinatura")
        
        # 2. Extrair informações relevantes
        status = detalhes_assinatura.get("status")
        payer_email = detalhes_assinatura.get("payer_email")
        plan_id = detalhes_assinatura.get("preapproval_plan_id")
        external_reference = detalhes_assinatura.get("external_reference")
        
        # 3. Verificar se é uma assinatura válida
        valid_statuses = ["authorized", "paused", "cancelled"]
        if status not in valid_statuses:
            raise ValueError(f"Status de assinatura inválido: {status}")
        
        # 4. Processar no banco de dados
        with db.begin() as connection:
            # Obter ou criar usuário
            usuario_id = obter_ou_criar_usuario(
                connection=connection,
                email=payer_email,
                external_reference=external_reference,
                plano=plan_id
            )
            
            # Processar assinatura
            processar_assinatura_db(
                connection=connection,
                subscription_id=subscription_id,
                usuario_id=usuario_id,
                status=status
            )
        
        registrar_log(
            payload=json.dumps(detalhes_assinatura),
            status_processamento='assinatura_processada',
            mensagem_erro=None
        )
        return jsonify({"status": "assinatura_processada"}), 200
        
    except Exception as e:
        erro_msg = f"Erro ao processar assinatura: {str(e)}"
        logging.error(erro_msg)
        registrar_log(
            payload=json.dumps(payload),
            status_processamento='erro_processamento',
            mensagem_erro=erro_msg
        )
        return jsonify({"erro": str(e)}), 400

def processar_pagamento(payload):
    try:
        payment_id = payload["data"]["id"]
        logging.info(f"Processando pagamento: {payment_id}")
        
        # 1. Obter detalhes do pagamento
        detalhes_pagamento = obter_detalhes_pagamento(payment_id)
        
        if not detalhes_pagamento:
            raise ValueError("Não foi possível obter detalhes do pagamento")
        
        # 2. Extrair informações relevantes
        status = detalhes_pagamento.get("status")
        valor = detalhes_pagamento.get("transaction_amount")
        email = detalhes_pagamento.get("payer", {}).get("email")
        external_reference = detalhes_pagamento.get("external_reference")
        payment_method = detalhes_pagamento.get("payment_method_id")
        data_aprovacao = detalhes_pagamento.get("date_approved")
        
        # 3. Verificar se o pagamento foi aprovado
        if status != "approved":
            logging.info(f"Pagamento {payment_id} não aprovado (status: {status})")
            return jsonify({"status": "ignorado", "motivo": "pagamento_nao_aprovado"}), 200
        
        # 4. Processar no banco de dados
        with db.begin() as connection:
            # Verificar duplicidade
            if verificar_pagamento_duplicado(connection, payment_id):
                logging.warning(f"Pagamento duplicado detectado: {payment_id}")
                return jsonify({"status": "ignorado", "motivo": "pagamento_duplicado"}), 200
            
            # Obter usuário
            usuario_id = obter_usuario_id(
                connection=connection,
                email=email,
                external_reference=external_reference
            )
            
            if not usuario_id:
                raise ValueError("Usuário não encontrado para este pagamento")
            
            # Registrar pagamento
            registrar_pagamento_db(
                connection=connection,
                payment_id=payment_id,
                status=status,
                valor=valor,
                metodo_pagamento=payment_method,
                data_pagamento=data_aprovacao,
                usuario_id=usuario_id
            )
            
            # Atualizar última geração do usuário
            if status == "approved":
                atualizar_ultima_geracao_usuario(
                    connection=connection,
                    usuario_id=usuario_id
                )
        
        registrar_log(
            payload=json.dumps(detalhes_pagamento),
            status_processamento='pagamento_processado',
            mensagem_erro=None
        )
        return jsonify({"status": "pagamento_processado"}), 200
        
    except Exception as e:
        erro_msg = f"Erro ao processar pagamento: {str(e)}"
        logging.error(erro_msg, exc_info=True)
        registrar_log(
            payload=json.dumps(payload),
            status_processamento='erro_processamento',
            mensagem_erro=erro_msg
        )
        return jsonify({"erro": str(e)}), 400

# Funções auxiliares para banco de dados
def obter_ou_criar_usuario(connection, email=None, external_reference=None, plano=None):
    """Obtém ou cria um usuário no banco de dados"""
    usuario_id = None
    
    # Tentar encontrar pelo external_reference (se existir)
    if external_reference:
        result = connection.execute(
            text("SELECT id FROM usuarios WHERE id = :external_reference"),
            {"external_reference": external_reference}
        )
        usuario = result.fetchone()
        if usuario:
            usuario_id = usuario[0]
    
    # Se não encontrou pelo external_reference, tentar pelo email
    if not usuario_id and email:
        result = connection.execute(
            text("SELECT id FROM usuarios WHERE email = :email"),
            {"email": email}
        )
        usuario = result.fetchone()
        if usuario:
            usuario_id = usuario[0]
    
    # Se não encontrou, criar novo usuário
    if not usuario_id:
        if not email:
            raise ValueError("Email é necessário para criar novo usuário")
        
        result = connection.execute(
            text("""
                INSERT INTO usuarios (
                    email,
                    data_inscricao,
                    ultima_geracao,
                    plano
                ) VALUES (
                    :email,
                    NOW(),
                    NOW(),
                    :plano
                ) RETURNING id
            """),
            {
                "email": email,
                "plano": plano
            }
        )
        usuario_id = result.fetchone()[0]
    else:
        # Atualizar plano se necessário
        if plano:
            connection.execute(
                text("UPDATE usuarios SET plano = :plano WHERE id = :usuario_id"),
                {"plano": plano, "usuario_id": usuario_id}
            )
    
    return usuario_id

def processar_assinatura_db(connection, subscription_id, usuario_id, status):
    """Processa a assinatura no banco de dados"""
    # Verificar se já existe uma assinatura para este usuário
    result = connection.execute(
        text("SELECT id FROM assinatura WHERE usuario_id = :usuario_id"),
        {"usuario_id": usuario_id}
    )
    assinatura_existente = result.fetchone()
    
    if assinatura_existente:
        # Atualizar assinatura existente
        connection.execute(
            text("""
                UPDATE assinatura SET
                    subscription_id = :subscription_id,
                    status = :status,
                    data_atualizacao = NOW()
                WHERE usuario_id = :usuario_id
            """),
            {
                "subscription_id": subscription_id,
                "status": status,
                "usuario_id": usuario_id
            }
        )
    else:
        # Criar nova assinatura
        connection.execute(
            text("""
                INSERT INTO assinatura (
                    subscription_id,
                    usuario_id,
                    status,
                    data_criacao,
                    data_atualizacao
                ) VALUES (
                    :subscription_id,
                    :usuario_id,
                    :status,
                    NOW(),
                    NOW()
                )
            """),
            {
                "subscription_id": subscription_id,
                "usuario_id": usuario_id,
                "status": status
            }
        )

def verificar_pagamento_duplicado(connection, payment_id):
    """Verifica se o pagamento já foi processado anteriormente"""
    result = connection.execute(
        text("SELECT id FROM pagamento WHERE payment_id = :payment_id"),
        {"payment_id": payment_id}
    )
    return result.fetchone() is not None

def registrar_pagamento_db(connection, payment_id, status, valor, metodo_pagamento, data_pagamento, usuario_id):
    """Registra o pagamento no banco de dados"""
    connection.execute(
        text("""
            INSERT INTO pagamento (
                payment_id,
                status,
                valor,
                metodo_pagamento,
                data_pagamento,
                usuario_id
            ) VALUES (
                :payment_id,
                :status,
                :valor,
                :metodo_pagamento,
                :data_pagamento,
                :usuario_id
            )
        """),
        {
            "payment_id": payment_id,
            "status": status,
            "valor": valor,
            "metodo_pagamento": metodo_pagamento,
            "data_pagamento": data_pagamento,
            "usuario_id": usuario_id
        }
    )

def atualizar_ultima_geracao_usuario(connection, usuario_id):
    """Atualiza a última geração do usuário"""
    connection.execute(
        text("UPDATE usuarios SET ultima_geracao = NOW() WHERE id = :usuario_id"),
        {"usuario_id": usuario_id}
    )

def obter_usuario_id(connection, email=None, external_reference=None):
    """Obtém o ID do usuário por email ou external_reference"""
    if external_reference:
        result = connection.execute(
            text("SELECT id FROM usuarios WHERE id = :external_reference"),
            {"external_reference": external_reference}
        )
        usuario = result.fetchone()
        if usuario:
            return usuario[0]
    
    if email:
        result = connection.execute(
            text("SELECT id FROM usuarios WHERE email = :email"),
            {"email": email}
        )
        usuario = result.fetchone()
        if usuario:
            return usuario[0]
    
    return None

# Funções para integração com API do Mercado Pago
def obter_detalhes_assinatura(id_assinatura):
    """Obtém detalhes da assinatura da API do Mercado Pago"""
    try:
        resposta = requests.get(
            f"https://api.mercadopago.com/preapproval/{id_assinatura}",
            headers={
                "Authorization": f"Bearer {os.getenv('MERCADO_PAGO_ACCESS_TOKEN')}",
                "Content-Type": "application/json"
            },
            timeout=15
        )
        resposta.raise_for_status()
        return resposta.json()
    except Exception as e:
        logging.error(f"Erro ao consultar assinatura {id_assinatura}: {str(e)}")
        return None

def obter_detalhes_pagamento(id_pagamento):
    """Obtém detalhes do pagamento da API do Mercado Pago"""
    try:
        resposta = requests.get(
            f"https://api.mercadopago.com/v1/payments/{id_pagamento}",
            headers={
                "Authorization": f"Bearer {os.getenv('MERCADO_PAGO_ACCESS_TOKEN')}",
                "Content-Type": "application/json"
            },
            timeout=15
        )
        resposta.raise_for_status()
        return resposta.json()
    except Exception as e:
        logging.error(f"Erro ao consultar pagamento {id_pagamento}: {str(e)}")
        return None

# ================================================
# INICIALIZAÇÃO
# ================================================

if __name__ == "__main__":
    from waitress import serve
    logging.info("Iniciando servidor TreinoRun...")
    serve(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        threads=4
    )