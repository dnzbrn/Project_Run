#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import logging
import re
import hmac
import hashlib
import uuid
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

        if not usuario:
            return False if plano == "anual" else True

        if plano == "anual":
            # Primeiro tenta verificar na tabela assinaturas
            assinatura = db.execute(
                text("""
                    SELECT status FROM assinaturas 
                    WHERE usuario_id = :usuario_id 
                    ORDER BY id DESC LIMIT 1
                """),
                {"usuario_id": usuario.id}
            ).fetchone()

            if assinatura and assinatura.status == "active":
                return True

            # Se não tiver na assinatura, verifica se no cadastro consta como plano anual
            if usuario.plano == "anual":
                return True

            return False

        elif plano == "gratuito":
            if usuario.ultima_geracao:
                ultima = datetime.strptime(str(usuario.ultima_geracao), "%Y-%m-%d %H:%M:%S")
                return (datetime.now() - ultima).days >= 30
            return True

        return False

    except Exception as e:
        logging.error(f"Erro na verificação de plano: {e}")
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
            result = db.execute(
                text("""
                    INSERT INTO usuarios (email, plano, data_inscricao, ultima_geracao)
                    VALUES (:email, :plano, :data_inscricao, :ultima_geracao)
                    RETURNING id
                """),
                {"email": email, "plano": plano, "data_inscricao": hoje, "ultima_geracao": hoje},
            )
            usuario_id = result.fetchone()[0]
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
                    <p><a href="https://treinorun.com.br/" style="background-color: #27ae60; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Criar Novo Treino</a></p>
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
        email = session.get("email")
        plano = session.get("plano", "gratuito")
        assinatura_ativa = session.get("assinatura_ativa", False)

        return render_template(
            "landing.html",
            email=email,
            plano=plano,
            assinatura_ativa=assinatura_ativa
        )

    except Exception as e:
        logging.error(f"Erro ao renderizar landing.html: {e}")
        return render_template_string(f"""
            <!DOCTYPE html>
            <html>
            <body>
                <h1>TreinoRun - Página Inicial</h1>
                <p>Aplicação está funcionando, mas o template não foi carregado.</p>
                <p><strong>Email na sessão:</strong> {session.get("email", "Nenhum")}</p>
                <p><strong>Plano:</strong> {session.get("plano", "gratuito")}</p>
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
        email = session.get("email")

        if not email:
            # Se o email não está na sessão, tenta recuperar da URL (opcional)
            email = request.args.get("email")

        if email:
            usuario = db.execute(
                text("SELECT * FROM usuarios WHERE email = :email"),
                {"email": email}
            ).fetchone()

            if usuario:
                # Garante que o plano e email estejam corretos na sessão
                session["plano"] = usuario.plano
                session["email"] = usuario.email  # Atualiza email também para garantir
            else:
                logging.warning(f"Usuário não encontrado para email: {email}")
        else:
            logging.warning("Nenhum email encontrado na sessão ou URL.")

        # Renderiza a página de sucesso normal
        return render_template("sucesso.html")

    except Exception as e:
        logging.error(f"Erro ao renderizar sucesso.html: {e}")
        return render_template_string("""
            <!DOCTYPE html>
            <html>
            <body>
                <h1>Sucesso!</h1>
                <p>Pagamento aprovado, mas ocorreu um erro ao carregar seus dados. Tente acessar novamente.</p>
                <a href="/">Voltar para a Página Inicial</a>
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
        plano = session.get("plano_gerado", "Nenhum plano gerado.")  # 🔥 Corrigido aqui!

        return render_template("resultado.html", titulo=titulo, plano=plano)
    except Exception as e:
        logging.error(f"Erro ao renderizar resultado.html: {e}")
        return f"""
        <!DOCTYPE html>
        <html>
        <body>
            <h1>{session.get('titulo', 'Plano de Treino')}</h1>
            <pre>{session.get('plano_gerado', 'Nenhum plano gerado.')}</pre>
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
    required_fields = ["email", "objetivo", "tempo_melhoria", "nivel", "dias", "tempo"]

    if not all(field in dados_usuario for field in required_fields):
        return "Dados do formulário incompletos.", 400

    email = dados_usuario["email"]
    plano = session.get("plano", dados_usuario.get("plano", "gratuito"))
    session["email"] = email

    if not pode_gerar_plano(email, plano):
        if plano == "anual":
            return redirect(url_for("iniciar_pagamento", email=email))
        return "Você já gerou um plano gratuito este mês. Atualize para o plano anual para gerar mais planos.", 400

    semanas = calcular_semanas(dados_usuario['tempo_melhoria'])

    prompt = prompt = f"""
Você é um treinador de corrida profissional.

Crie um plano de corrida para que o usuário atinja o objetivo: {dados_usuario['objetivo']} em {dados_usuario['tempo_melhoria']}.

✅ Dados do usuário:
- Nível: {dados_usuario['nivel']}
- Dias disponíveis por semana: {dados_usuario['dias']}
- Tempo disponível por treino: {dados_usuario['tempo']} minutos
- Duração do plano: {semanas} semanas

✅ Instruções:
- Se o plano tiver **até 12 semanas**, **detalhe TODAS as semanas separadamente** (não pule, não agrupe, não use "...").
- Se o plano tiver **mais de 12 semanas**, **detalhe até a semana 8** e depois **agrupá-las** (ex.: "Semanas 9–12: continuar aumentando volume e intensidade").
- Cada treino deve conter:
  - Aquecimento (com tempo e atividade sugerida, ex.: "10 min de caminhada rápida").
  - Parte principal (distância e ritmo indicados, ex.: "4x800m a 5:30/km").
  - Desaquecimento (ex.: "5 min de caminhada leve").
- Na **semana final (semana do objetivo)**, criar um treino especial para tentar atingir o objetivo, sugerindo ritmo (ex.: "Correr 5km a 6:00/km").
- Escreva dicas finais de recuperação e motivação no final.

✅ Formato de saída:
- Título principal: **Plano de Corrida para {dados_usuario['objetivo']}**
- Informações do usuário
- Semana a semana (ex.: Semana 1, Semana 2, etc.)
- Semana final (objetivo)
- Dicas finais

✅ Estilo de escrita:
- Profissional, claro e motivador.
- Nunca usar comandos internos ou instruções técnicas.
"""

    # 🔥 Faltava isso:
    plano_gerado = await gerar_plano_openai(prompt, semanas)

    if registrar_geracao(email, plano):
        session["titulo"] = f"Plano de Corrida: {dados_usuario['objetivo']}"
        session["plano"] = plano
        session["plano_gerado"] = "Este plano é gerado automaticamente. Consulte um profissional para ajustes.\n\n" + plano_gerado
        return redirect(url_for("resultado"))
    else:
        return "Erro ao registrar seu plano. Tente novamente.", 500




@app.route("/generatePace", methods=["POST"])
@limiter.limit("10 per hour")
@async_route
async def generatePace():
    dados_usuario = request.form
    required_fields = ["email", "objetivo", "tempo_melhoria", "nivel", "dias", "tempo"]

    if not all(field in dados_usuario for field in required_fields):
        return "Dados do formulário incompletos.", 400

    email = dados_usuario["email"]
    plano = session.get("plano", dados_usuario.get("plano", "gratuito"))
    session["email"] = email

    if not pode_gerar_plano(email, plano):
        if plano == "anual":
            return redirect(url_for("iniciar_pagamento", email=email))
        return "Você já gerou um plano gratuito este mês. Atualize para o plano anual para gerar mais planos.", 400

    semanas = calcular_semanas(dados_usuario['tempo_melhoria'])

    prompt = f"""
Você é um treinador de corrida especializado em melhoria de pace.

Crie um plano para que o usuário alcance o objetivo: {dados_usuario['objetivo']} em {dados_usuario['tempo_melhoria']}.

✅ Dados do usuário:
- Nível: {dados_usuario['nivel']}
- Dias disponíveis por semana: {dados_usuario['dias']}
- Tempo disponível por treino: {dados_usuario['tempo']} minutos
- Tipo de treino: Foco em pace (ritmo)
- Duração do plano: {semanas} semanas

✅ Instruções:
- Se o plano tiver **até 12 semanas**, **detalhar todas as semanas separadamente** (sem pular, sem usar "..." ou "(detalhar depois)").
- Se o plano tiver **mais de 12 semanas**, **detalhar até a semana 8** e depois **agrupar as demais** (ex.: "Semanas 9–12: foco em aumento progressivo de distância e redução gradual do pace").
- Em cada treino:
  - Aquecimento inicial (ex.: 5-10 minutos de caminhada rápida ou trote leve).
  - Parte principal com distâncias e ritmos claros (ex.: "6x400m a 5:30/km").
  - Desaquecimento (ex.: 5-10 minutos de caminhada leve).
- Na **semana final**, montar um treino especial tentando atingir o ritmo objetivo para a distância desejada, indicando ritmo sugerido (ex.: "Correr 5km a 5:00/km").

✅ Formato de saída:
- Título principal: **Plano de Treino para Melhorar Pace: {dados_usuario['objetivo']}**
- Informações iniciais do usuário
- Semana a semana detalhada
- Semana final (treino do objetivo)
- Dicas finais de recuperação e motivação

✅ Estilo de escrita:
- Profissional, amigável e motivador.
- Não usar comandos internos ou instruções técnicas.
"""

    # 🔥 Faltava isso também no seu código:
    plano_gerado = await gerar_plano_openai(prompt, semanas)

    if registrar_geracao(email, plano):
        session["titulo"] = f"Plano de Pace: {dados_usuario['objetivo']}"
        session["plano"] = plano
        session["plano_gerado"] = "Este plano é gerado automaticamente. Consulte um profissional para ajustes.\n\n" + plano_gerado
        return redirect(url_for("resultado"))
    else:
        return "Erro ao registrar seu plano. Tente novamente.", 500




@app.route("/send_plan_email", methods=["POST"])
@limiter.limit("10 per minute")
def send_plan_email():
    try:
        data = request.get_json()

        email_destino = data.get("email")
        pdf_data_uri = data.get("pdfData")

        if not email_destino or not pdf_data_uri:
            return jsonify({"message": "Dados incompletos"}), 400

        # Extrair título do treino salvo na sessão
        titulo_treino = session.get("titulo", "Seu Plano de Treino")

        # Determinar se é treino de Pace ou Corrida
        if "pace" in titulo_treino.lower():
            descricao = "Seu plano personalizado para melhorar seu pace!"
        elif "corrida" in titulo_treino.lower() or "correr" in titulo_treino.lower():
            descricao = "Seu plano personalizado para sua corrida!"
        else:
            descricao = "Seu treino personalizado está pronto!"

        # Converter base64 para bytes
        header, encoded = pdf_data_uri.split(",", 1)
        pdf_bytes = base64.b64decode(encoded)

        # Criar mensagem HTML com cores da landing (verde e azul)
        corpo_html = f"""
        <!DOCTYPE html>
        <html lang="pt-BR">
        <body style="font-family: Arial, sans-serif; background-color: #f0fdf4; padding: 20px;">
          <div style="background-color: #ffffff; padding: 30px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1); max-width: 600px; margin: auto;">
            <h2 style="color: #22c55e; text-align: center;">🏃‍♂️ {titulo_treino}</h2>
            <p style="text-align: center; font-size: 18px; color: #374151;">{descricao}</p>
            <p style="margin-top: 20px; font-size: 16px; color: #4b5563;">Segue em anexo o seu plano de treino, feito especialmente para você atingir seus objetivos.</p>
            <div style="text-align: center; margin: 30px 0;">
              <a href="https://treinorun.com.br/seutreino" style="background-color: #3b82f6; color: #ffffff; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: bold;">Criar Novo Treino</a>
            </div>
            <p style="font-size: 14px; color: #9ca3af; text-align: center;">Bons treinos!<br>Equipe TreinoRun</p>
          </div>
        </body>
        </html>
        """

        # Criar e-mail
        msg = Message(
            subject=f"🏃 {titulo_treino} - TreinoRun",
            recipients=[email_destino],
            html=corpo_html,
        )

        # Anexar o PDF
        msg.attach(
            filename=f"Plano_Treino_{datetime.now().strftime('%Y%m%d')}.pdf",
            content_type="application/pdf",
            data=pdf_bytes
        )

        # Enviar
        mail.send(msg)

        logging.info(f"E-mail de treino enviado para {email_destino}")
        return jsonify({"message": "E-mail enviado com sucesso"}), 200

    except Exception as e:
        logging.error(f"Erro ao enviar treino por e-mail: {str(e)}", exc_info=True)
        return jsonify({"message": "Erro interno ao enviar e-mail"}), 500

@app.route("/login", methods=["POST"])
def login():
    email = request.form.get("email")

    if not email:
        return redirect(url_for('landing'))

    usuario = db.execute(
        text("SELECT * FROM usuarios WHERE email = :email"),
        {"email": email}
    ).fetchone()

    assinatura_ativa = False
    if usuario:
        # Verifica na tabela assinaturas se tem ativa
        assinatura = db.execute(
            text("""
                SELECT status FROM assinaturas 
                WHERE usuario_id = :usuario_id 
                ORDER BY id DESC LIMIT 1
            """),
            {"usuario_id": usuario.id}
        ).fetchone()

        if (assinatura and assinatura.status == "active") or usuario.plano == "anual":
            assinatura_ativa = True

    session["email"] = email
    session["plano"] = "anual" if assinatura_ativa else "gratuito"
    session["assinatura_ativa"] = assinatura_ativa

    return redirect(url_for('landing'))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('landing'))





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

    session["email"] = email	

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
        logging.info(f"Webhook recebido - IP: {request.remote_addr}")

        # 2. Verificar se é uma notificação de teste simples
        dados_brutos = request.data.decode('utf-8')
        if dados_brutos.strip() == 'TEST_NOTIFICATION':
            logging.info("Notificação de teste recebida")
            registrar_log(
                payload=dados_brutos,
                status_processamento='teste',
                mensagem_erro=None
            )
            return jsonify({"status": "teste_recebido"}), 200

        # 3. Processar o payload JSON
        try:
            payload = request.get_json(force=True)  # Força pegar o JSON mesmo sem header correto
            if not payload:
                raise ValueError("Payload vazio")
        except Exception as e:
            erro_msg = f"Payload JSON inválido: {str(e)}"
            logging.error(erro_msg)
            registrar_log(
                payload=dados_brutos,
                status_processamento='erro',
                mensagem_erro=erro_msg
            )
            return jsonify({"erro": "JSON inválido"}), 400

        # 4. Registrar o payload recebido
        registrar_log(
            payload=json.dumps(payload),
            status_processamento='recebido',
            mensagem_erro=None
        )

        # 5. Identificar e processar conforme o tipo
        tipo = payload.get("type")
        action = payload.get("action", "")

        if tipo == "subscription_preapproval":
            return processar_assinatura(payload)

        elif tipo == "payment":
            payment_id = payload.get("data", {}).get("id")
            if not payment_id:
                erro_msg = "ID do pagamento não encontrado no payload."
                logging.error(erro_msg)
                registrar_log(
                    payload=json.dumps(payload),
                    status_processamento='erro',
                    mensagem_erro=erro_msg
                )
                return jsonify({"erro": "ID do pagamento não encontrado"}), 400

            return processar_pagamento(payload)  # AQUI corrigido: envia payload inteiro

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
        id_assinatura = payload["data"]["id"]
        logging.info(f"Processando assinatura: {id_assinatura}")

        status = payload.get("action", "updated")
        email = payload.get("payer_email")

        # ⚡ Inserir ou atualizar assinatura
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO assinaturas (subscription_id, status, data_atualizacao)
                    VALUES (:subscription_id, :status, NOW())
                    ON CONFLICT (subscription_id) DO UPDATE
                    SET status = EXCLUDED.status,
                        data_atualizacao = NOW()
                """),
                {
                    "subscription_id": id_assinatura,
                    "status": status
                }
            )

        # ⚡ Atualiza ou cria o usuário se for assinatura real
        if email:
            with engine.begin() as conn:
                usuario = conn.execute(
                    text("SELECT * FROM usuarios WHERE email = :email"),
                    {"email": email}
                ).fetchone()

                if not usuario:
                    conn.execute(
                        text("""
                            INSERT INTO usuarios (email, plano, data_inscricao, ultima_geracao)
                            VALUES (:email, 'anual', NOW(), NOW())
                        """),
                        {"email": email}
                    )
                else:
                    conn.execute(
                        text("""
                            UPDATE usuarios
                            SET plano = 'anual', data_inscricao = NOW()
                            WHERE email = :email
                        """),
                        {"email": email}
                    )
            enviar_email_confirmacao_pagamento(email)

        registrar_log(
            payload=json.dumps(payload),
            status_processamento='assinatura_processada',
            mensagem_erro=None
        )
        return jsonify({"status": "assinatura_processada"}), 200

    except Exception as e:
        erro_msg = f"Erro ao processar assinatura: {str(e)}"
        logging.error(erro_msg, exc_info=True)
        registrar_log(
            payload=json.dumps(payload),
            status_processamento='erro_processamento',
            mensagem_erro=erro_msg
        )
        return jsonify({"erro": str(e)}), 400

def obter_detalhes_assinatura(subscription_id):
    try:
        resposta = requests.get(
            f"https://api.mercadopago.com/preapproval/{subscription_id}",
            headers={
                "Authorization": f"Bearer {os.getenv('MERCADO_PAGO_ACCESS_TOKEN')}",
                "Content-Type": "application/json"
            },
            timeout=15
        )
        resposta.raise_for_status()
        return resposta.json()
    except Exception as e:
        logging.error(f"Erro ao consultar assinatura {subscription_id}: {str(e)}")
        return None

def processar_pagamento(payload):
    try:
        # 1. Verificar se é um evento de teste
        is_test = not payload.get("live_mode", True)

        # 2. Pegar ID do pagamento (de forma segura)
        id_pagamento = payload.get("data", {}).get("id")

        if not id_pagamento:
            logging.info("Webhook de pagamento sem ID recebido. Considerando como teste.")
            registrar_log(
                payload=json.dumps(payload),
                status_processamento='pagamento_teste_sem_id',
                mensagem_erro="ID de pagamento ausente"
            )
            return jsonify({"status": "pagamento_teste_sem_id_recebido"}), 200

        if is_test:
            # ⚡ TESTE: Simula pagamento sem consultar Mercado Pago
            logging.info(f"Recebido pagamento de teste com ID {id_pagamento} (não consultando API Mercado Pago)")

            status = "approved"  # Simula aprovado
            email = "teste@exemplo.com"  # Simula um email

        else:
            # ⚡ Se não for teste: pega detalhes reais
            logging.info(f"Processando pagamento real: {id_pagamento}")

            detalhes_pagamento = obter_detalhes_pagamento(id_pagamento)
            if not detalhes_pagamento:
                raise ValueError("Não foi possível obter detalhes do pagamento")

            status = detalhes_pagamento.get("status")
            email = detalhes_pagamento.get("payer", {}).get("email")

            if not email:
                raise ValueError("Email não encontrado no pagamento")

        # ⚡ Atualiza pagamento real (ou teste) no banco
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO pagamentos (payment_id, status, data_pagamento)
                    VALUES (:payment_id, :status, NOW())
                    ON CONFLICT (payment_id) DO UPDATE
                    SET status = EXCLUDED.status,
                        data_pagamento = NOW()
                """),
                {
                    "payment_id": id_pagamento,
                    "status": status
                }
            )

        # ⚡ Atualiza ou cria o usuário se pagamento aprovado
        if status == "approved" and email:
            with engine.begin() as conn:
                usuario = conn.execute(
                    text("SELECT * FROM usuarios WHERE email = :email"),
                    {"email": email}
                ).fetchone()

                if not usuario:
                    conn.execute(
                        text("""
                            INSERT INTO usuarios (email, plano, data_inscricao, ultima_geracao)
                            VALUES (:email, 'anual', NOW(), NOW())
                        """),
                        {"email": email}
                    )
                else:
                    conn.execute(
                        text("""
                            UPDATE usuarios
                            SET plano = 'anual', data_inscricao = NOW()
                            WHERE email = :email
                        """),
                        {"email": email}
                    )
            enviar_email_confirmacao_pagamento(email)

        registrar_log(
            payload=json.dumps(payload),
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



def obter_detalhes_pagamento(id_pagamento):
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
