#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import logging
import re
import hmac
import hashlib
import threading
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO
import base64
import json

from flask import Flask, request, render_template, render_template_string, redirect, url_for, session, jsonify, abort
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

# ----------------------------------------------
# MIDDLEWARE PARA LOGS 
# ----------------------------------------------
@app.before_request
def log_request_info():
    if request.path == "/webhook/mercadopago":
        try:
            logging.info(f"\n📨 Headers recebidos:\n{json.dumps(dict(request.headers), indent=2)}")
            logging.info(f"📦 Body recebido:\n{request.data.decode('utf-8', errors='replace')}")
        except Exception as e:
            logging.error(f"Erro ao logar requisição: {str(e)}")

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

# ProxyFix para ambientes com proxy reverso
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

# ID do plano de assinatura no Mercado Pago
MERCADOPAGO_PLAN_ID = "2c93808494b46ea50194bee12d88057e"

# ================================================
# FUNÇÕES AUXILIARES (COM CORREÇÕES NO WEBHOOK)
# ================================================

def validar_assinatura(body, signature):
    try:
        # 1. Extrai a parte v1= da assinatura (novo formato MP)
        if "v1=" in signature:
            signature = signature.split("v1=")[1].split(",")[0]
        
        # 2. Remove possíveis prefixos residuais
        signature = signature.replace("sha256=", "").strip()
        
        # 3. Validação HMAC
        chave_secreta = os.getenv("MERCADO_PAGO_WEBHOOK_SECRET", "").encode('utf-8')
        if not chave_secreta:
            logging.error("❌ Chave secreta não configurada")
            return False
            
        assinatura_calculada = hmac.new(chave_secreta, body, hashlib.sha256).hexdigest()
        
        # 4. Logs para diagnóstico
        logging.info(f"🔑 Assinatura recebida: {signature}")
        logging.info(f"🔑 Assinatura calculada: {assinatura_calculada}")
        
        return hmac.compare_digest(assinatura_calculada, signature)
        
    except Exception as e:
        logging.error(f"💥 Erro na validação: {str(e)}")
        return False

def processar_notificacao_assinatura(payload):
    """Processa notificação de assinatura em background"""
    try:
        subscription_id = payload["data"]["id"]
        subscription_data = obter_detalhes_assinatura(subscription_id)
        
        if not subscription_data:
            logging.error(f"Detalhes não encontrados para assinatura {subscription_id}")
            return
            
        email = subscription_data.get("payer_email") or subscription_data.get("external_reference")
        status = subscription_data.get("status")
        
        if email and status:
            usuario = db.execute(
                text("SELECT id FROM usuarios WHERE email = :email"),
                {"email": email}
            ).fetchone()
            
            if usuario:
                db.execute(
                    text("""
                        UPDATE assinaturas 
                        SET status = :status, 
                            data_atualizacao = NOW() 
                        WHERE subscription_id = :subscription_id
                    """),
                    {"status": status, "subscription_id": subscription_id}
                )
                
                if status == "authorized":
                    enviar_email_confirmacao_pagamento(email)
                
                db.commit()
                logging.info(f"Assinatura {subscription_id} atualizada para {status}")
    except Exception as e:
        logging.error(f"Erro no processamento: {e}")
        db.rollback()

# ================================================
# ROTA DE WEBHOOK CORRIGIDA (MÉTODOS GET + POST)
# ================================================

@app.route("/webhook/mercadopago", methods=["GET", "POST"])
@limiter.limit("100 per day")
def mercadopago_webhook():
    logging.info(f"Headers recebidos: {dict(request.headers)}")  # ← Adicione esta linha
    try:
        # Verificação inicial do Mercado Pago (GET)
        if request.method == "GET":
            return jsonify({"status": "ok"}), 200

        # Validação da assinatura
        signature = request.headers.get("X-Signature", "")
        if not validar_assinatura(request.data, signature):
            logging.warning(f"Assinatura inválida: {signature}")
            return jsonify({"error": "Unauthorized"}), 401

        payload = request.get_json()
        logging.info(f"Webhook recebido: {json.dumps(payload, indent=2)}")
        
        # Registrar log inicial
        db.execute(
            text("""
                INSERT INTO logs_webhook (
                    data_recebimento,
                    payload,
                    status_processamento
                ) VALUES (
                    NOW(),
                    :payload,
                    'recebido'
                )
            """),
            {"payload": json.dumps(payload)}
        )
        db.commit()
        
        # Processar em background
        if payload.get("type") == "subscription_preapproval":
            threading.Thread(
                target=processar_notificacao_assinatura,
                args=(payload,)
            ).start()

        return jsonify({"status": "received"}), 200
        
    except Exception as e:
        logging.error(f"Erro no webhook: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# ================================================
# TODAS AS OUTRAS ROTAS ORIGINAIS (IDÊNTICAS)
# ================================================

@app.route("/")
@limiter.limit("100 per hour")
def landing():
    try:
        return render_template("landing.html")
    except Exception as e:
        logging.error(f"Erro ao renderizar landing.html: {e}")
        return """
        <!DOCTYPE html>
        <html>
        <body>
            <h1>TreinoRun - Página Inicial</h1>
            <p>Aplicação está funcionando, mas o template não foi carregado.</p>
            <a href="/seutreino">Criar Treino</a>
        </body>
        </html>
        """, 200

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
        return """
        <!DOCTYPE html>
        <html>
        <body>
            <h1>Criar Seu Treino</h1>
            <p>Formulário de criação de treino</p>
        </body>
        </html>
        """, 200

@app.route("/sucesso")
@limiter.limit("100 per hour")
def sucesso():
    try:
        return render_template("sucesso.html")
    except Exception as e:
        logging.error(f"Erro ao renderizar sucesso.html: {e}")
        return """
        <!DOCTYPE html>
        <html>
        <body>
            <h1>Sucesso!</h1>
            <p>Operação concluída com sucesso.</p>
        </body>
        </html>
        """, 200

@app.route("/erro")
@limiter.limit("100 per hour")
def erro():
    try:
        return render_template("erro.html")
    except Exception as e:
        logging.error(f"Erro ao renderizar erro.html: {e}")
        return """
        <!DOCTYPE html>
        <html>
        <body>
            <h1>Erro</h1>
            <p>Ocorreu um erro inesperado.</p>
        </body>
        </html>
        """, 200

@app.route("/pendente")
@limiter.limit("100 per hour")
def pendente():
    try:
        return render_template("pendente.html")
    except Exception as e:
        logging.error(f"Erro ao renderizar pendente.html: {e}")
        return """
        <!DOCTYPE html>
        <html>
        <body>
            <h1>Pagamento Pendente</h1>
            <p>Seu pagamento está sendo processado.</p>
        </body>
        </html>
        """, 200

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

@app.route('/artigos/melhorar-pace')
@limiter.limit("100 per hour")
def artigo_pace():
    return render_template('artigos/artigo-melhorar-pace.html')

@app.route('/artigos/tenis-corrida')
@limiter.limit("100 per hour")
def artigo_tenis():
    return render_template('artigos/artigo-tenis-corrida.html')

@app.route('/artigos/alongamento')
@limiter.limit("100 per hour")
def artigo_alongamento():
    return render_template('artigos/artigo-alongamento.html')

@app.route('/artigos/alimentacao')
@limiter.limit("100 per hour")
def artigo_alimentacao():
    return render_template('artigos/artigo-alimentacao.html')

@app.route("/iniciar_assinatura", methods=["POST"])
@limiter.limit("5 per hour")
def iniciar_assinatura():
    dados_usuario = request.form
    
    if "email" not in dados_usuario:
        return "Email não fornecido.", 400

    email = dados_usuario["email"]
    nome = dados_usuario.get("nome", "Cliente")

    try:
        # Registrar tentativa no banco de dados
        db.execute(
            text("INSERT INTO tentativas_pagamento (email, data_tentativa) VALUES (:email, NOW())"),
            {"email": email}
        )
        db.commit()
    except Exception as e:
        logging.error(f"Erro ao registrar tentativa: {e}")
        db.rollback()

    # Criar payload para assinatura
    payload = {
        "preapproval_plan_id": MERCADOPAGO_PLAN_ID,
        "payer_email": email,
        "external_reference": email,
        "back_url": url_for("sucesso", _external=True),
        "notification_url": url_for("mercadopago_webhook", _external=True),
        "payer": {
            "name": nome,
            "email": email
        }
    }

    try:
        # Criar assinatura no Mercado Pago
        response = requests.post(
            "https://api.mercadopago.com/preapproval",
            headers={
                "Authorization": f"Bearer {os.getenv('MERCADO_PAGO_ACCESS_TOKEN')}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10
        )
        response.raise_for_status()
        
        subscription_data = response.json()
        
        # Registrar a assinatura no banco de dados (status inicial como pending)
        usuario = db.execute(
            text("""
                INSERT INTO usuarios (email, plano, data_inscricao)
                VALUES (:email, 'anual', NOW())
                ON CONFLICT (email) 
                DO UPDATE SET plano = 'anual'
                RETURNING id
            """),
            {"email": email}
        ).fetchone()
        
        db.execute(
            text("""
                INSERT INTO assinaturas (
                    subscription_id, 
                    usuario_id, 
                    status, 
                    data_atualizacao
                ) VALUES (
                    :subscription_id, 
                    :usuario_id, 
                    :status, 
                    NOW()
                )
            """),
            {
                "subscription_id": subscription_data['id'],
                "usuario_id": usuario[0],
                "status": "pending"
            }
        )
        db.commit()
        
        # Redirecionar para o checkout
        return redirect(subscription_data['init_point'])
        
    except requests.exceptions.RequestException as e:
        logging.error(f"Erro ao criar assinatura: {str(e)}")
        if hasattr(e, 'response'):
            logging.error(f"Resposta do Mercado Pago: {e.response.text}")
        return "Erro ao processar a assinatura. Tente novamente mais tarde.", 500
    except Exception as e:
        db.rollback()
        logging.error(f"Erro ao registrar assinatura: {str(e)}")
        return "Erro interno ao processar sua assinatura.", 500

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
            return redirect(url_for("iniciar_assinatura"))
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