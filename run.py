#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import logging
import re
import hmac
import hashlib
import socket
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO
import base64
import json

from flask import Flask, request, render_template, redirect, url_for, session, jsonify, abort
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
# CONFIGURAÇÃO INICIAL PARA PRODUÇÃO
# ================================================

app = Flask(__name__, template_folder='Templates')
app.secret_key = os.environ["FLASK_SECRET_KEY"]

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
socket.setdefaulttimeout(60)

# ================================================
# CONFIGURAÇÃO DE LOGGING
# ================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ================================================
# PROTEÇÃO CONTRA DDoS
# ================================================

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri=os.getenv("REDIS_URL", "memory://"),
    strategy="moving-window",
    default_limits=["500 per day", "100 per hour"]
)

@app.before_request
def security_checks():
    user_agent = request.headers.get('User-Agent', '').lower()
    bad_agents = ['nmap', 'sqlmap', 'nikto', 'metasploit']
    if any(agent in user_agent for agent in bad_agents):
        logger.warning(f"Bad User-Agent blocked: {user_agent}")
        abort(403)

# ================================================
# BANCO DE DADOS E SERVIÇOS
# ================================================

engine = create_engine(
    os.environ["DATABASE_URL"],
    pool_size=15,
    max_overflow=5,
    pool_pre_ping=True,
    pool_recycle=300
)
db = scoped_session(sessionmaker(bind=engine))

client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    timeout=30.0
)

app.config['MAIL_SERVER'] = 'smtp.zoho.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ['ZOHO_EMAIL']
app.config['MAIL_PASSWORD'] = os.environ['ZOHO_PASSWORD']
app.config['MAIL_DEFAULT_SENDER'] = ('TreinoRun', os.environ['ZOHO_EMAIL'])
mail = Mail(app)

# ================================================
# FUNÇÕES AUXILIARES COMPLETAS
# ================================================

def validar_assinatura(body, signature):
    chave_secreta = os.environ["MERCADO_PAGO_WEBHOOK_SECRET"].encode("utf-8")
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
        logger.error(f"Erro ao verificar permissão: {e}")
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
        logger.error(f"Erro ao calcular semanas: {e}")
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
                """),
                {"email": email, "plano": plano, "data_inscricao": hoje, "ultima_geracao": hoje},
            )
            usuario_id = db.execute(
                text("SELECT id FROM usuarios WHERE email = :email"), 
                {"email": email}
            ).fetchone()[0]
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
    except Exception as e:
        logger.error(f"Erro ao registrar geração: {e}")
        db.rollback()

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
        logger.error(f"Erro ao gerar plano: {e}")
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
                    .header {{ background-color: #27ae60; color: white; padding: 20px; }}
                </style>
            </head>
            <body>
                <div class="header">
                    <h2>Pagamento Confirmado!</h2>
                </div>
                <p>Olá {nome},</p>
                <p>Seu pagamento foi processado com sucesso!</p>
            </body>
            </html>
            """
        )
        mail.send(msg)
        logger.info(f"E-mail enviado para {email}")
    except Exception as e:
        logger.error(f"Erro ao enviar e-mail: {e}")

# ================================================
# ROTAS COMPLETAS
# ================================================

@app.route("/")
def landing():
    return render_template("landing.html")

@app.route("/seutreino")
def seutreino():
    return render_template("seutreino.html")

@app.route("/generate", methods=["POST"])
@limiter.limit("10 per hour")
async def generate():
    try:
        dados = request.form
        required = ["email", "plano", "objetivo", "tempo_melhoria", "nivel", "dias", "tempo"]
        
        if not all(field in dados for field in required):
            abort(400, description="Dados incompletos")

        email = dados["email"]
        plano = dados["plano"]

        if not pode_gerar_plano(email, plano):
            if plano == "anual":
                return redirect(url_for("iniciar_pagamento", email=email))
            abort(429, description="Limite de planos gratuitos atingido")

        semanas = calcular_semanas(dados['tempo_melhoria'])
        
        if "maratona" in dados['objetivo'].lower() and semanas < 16:
            abort(400, description="Maratona requer mínimo de 16 semanas")
        elif "meia-maratona" in dados['objetivo'].lower() and semanas < 12:
            abort(400, description="Meia-maratona requer mínimo de 12 semanas")

        prompt = f"""
        Crie um plano de corrida detalhado para: {dados['objetivo']} 
        em {dados['tempo_melhoria']}. Nível: {dados['nivel']}. 
        Dias/semana: {dados['dias']}. Tempo/sessão: {dados['tempo']} minutos.
        """

        plano_gerado = await gerar_plano_openai(prompt, semanas)
        registrar_geracao(email, plano)

        session["titulo"] = f"Plano para: {dados['objetivo']}"
        session["plano"] = "Este plano é gerado automaticamente. Consulte um profissional para ajustes.\n\n" + plano_gerado
        
        return redirect(url_for("resultado"))

    except Exception as e:
        logger.error(f"Erro em /generate: {str(e)}")
        abort(500)

@app.route("/resultado")
def resultado():
    titulo = session.get("titulo", "Plano de Treino")
    plano = session.get("plano", "Nenhum plano gerado.")
    return render_template("resultado.html", titulo=titulo, plano=plano)

@app.route("/iniciar_pagamento", methods=["GET", "POST"])
@limiter.limit("5 per hour")
def iniciar_pagamento():
    try:
        email = request.args.get("email") or request.form.get("email")
        if not email:
            abort(400, description="Email não fornecido")

        payload = {
            "items": [{
                "title": "Plano Anual TreinoRun",
                "quantity": 1,
                "unit_price": 59.9,
                "currency_id": "BRL",
            }],
            "payer": {"email": email},
            "back_urls": {
                "success": "https://treinorun.com.br/sucesso",
                "failure": "https://treinorun.com.br/erro",
                "pending": "https://treinorun.com.br/pendente",
            },
            "auto_return": "approved",
            "notification_url": "https://treinorun.com.br/webhook/mercadopago",
        }

        response = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers={
                "Authorization": f"Bearer {os.environ['MERCADO_PAGO_ACCESS_TOKEN']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10
        )
        response.raise_for_status()
        
        return redirect(response.json()["init_point"])
        
    except Exception as e:
        logger.error(f"Erro em /iniciar_pagamento: {str(e)}")
        abort(500)

@app.route("/webhook/mercadopago", methods=["POST"])
@limiter.limit("100 per day")
def mercadopago_webhook():
    try:
        if not validar_assinatura(request.data, request.headers.get("X-Signature")):
            abort(403, description="Assinatura inválida")

        payload = request.json
        evento = payload.get("action")
        
        if evento == "payment.updated":
            payment_id = payload["data"]["id"]
            status = payload["data"]["status"]
            
            if not db.execute(text("SELECT 1 FROM pagamentos WHERE payment_id = :payment_id"), 
                           {"payment_id": payment_id}).fetchone():
                db.execute(
                    text("INSERT INTO pagamentos (payment_id, status, data_pagamento) VALUES (:payment_id, :status, NOW())"),
                    {"payment_id": payment_id, "status": status},
                )

        elif evento == "subscription.updated":
            subscription_id = payload["data"]["id"]
            status = payload["data"]["status"]
            email = payload["data"]["payer"]["email"]
            nome = payload["data"]["payer"].get("first_name", "Cliente")

            usuario = db.execute(
                text("SELECT id FROM usuarios WHERE email = :email"), 
                {"email": email}
            ).fetchone()
            
            if not usuario:
                db.execute(
                    text("INSERT INTO usuarios (email, data_inscricao) VALUES (:email, NOW()) RETURNING id"), 
                    {"email": email}
                )
                usuario_id = db.fetchone()[0]
            else:
                usuario_id = usuario[0]

            db.execute(
                text("""
                    INSERT INTO assinaturas (subscription_id, usuario_id, status, data_atualizacao)
                    VALUES (:subscription_id, :usuario_id, :status, NOW())
                    ON CONFLICT (subscription_id) DO UPDATE
                    SET status = EXCLUDED.status, data_atualizacao = NOW()
                """),
                {"subscription_id": subscription_id, "usuario_id": usuario_id, "status": status},
            )
            
            if status == "active":
                enviar_email_confirmacao_pagamento(email, nome)

        db.commit()
        return jsonify({"status": "success"}), 200

    except Exception as e:
        logger.error(f"Erro em /webhook/mercadopago: {str(e)}")
        db.rollback()
        return jsonify({"error": str(e)}), 500

# ================================================
# INICIALIZAÇÃO PARA PRODUÇÃO
# ================================================

if __name__ == "__main__":
    from waitress import serve
    serve(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        threads=8,
        channel_timeout=60
    )