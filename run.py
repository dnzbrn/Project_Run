#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import logging
import hmac
import hashlib
import threading
import json
from datetime import timedelta
from functools import wraps

from flask import Flask, request, render_template, redirect, url_for, session, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker
from flask_mail import Mail, Message
import requests

# ================================================
# CONFIGURAÇÕES INICIAIS
# ================================================

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

app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    JSONIFY_PRETTYPRINT_REGULAR=False
)

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri=os.getenv("REDIS_URL", "memory://"),
    strategy="moving-window",
    default_limits=["200 per day", "50 per hour"]
)

# Banco de Dados
engine = create_engine(os.getenv("DATABASE_URL"), pool_size=10, max_overflow=0, pool_recycle=3600, pool_pre_ping=True)
db = scoped_session(sessionmaker(bind=engine))

# Configuração de e-mail
app.config.update(
    MAIL_SERVER='smtp.zoho.com',
    MAIL_PORT=587,
    MAIL_USE_TLS=True,
    MAIL_USERNAME=os.getenv('ZOHO_EMAIL'),
    MAIL_PASSWORD=os.getenv('ZOHO_PASSWORD'),
    MAIL_DEFAULT_SENDER=('TreinoRun', os.getenv('ZOHO_EMAIL'))
)
mail = Mail(app)

# Variáveis Mercado Pago
MERCADOPAGO_PLAN_ID = os.getenv("MERCADOPAGO_PLAN_ID")
MERCADOPAGO_ACCESS_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN")
MERCADO_PAGO_WEBHOOK_SECRET = os.getenv("MERCADO_PAGO_WEBHOOK_SECRET").encode('utf-8')

# ================================================
# FUNÇÕES AUXILIARES
# ================================================

def async_route(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(f(*args, **kwargs))
        finally:
            loop.close()
    return wrapper

def validar_assinatura(body, signature):
    try:
        body_normalizado = json.dumps(json.loads(body.decode('utf-8')), separators=(',', ':')).encode('utf-8')
        signature_value = signature.split("v1=")[1].split(",")[0] if "v1=" in signature else signature.replace("sha256=", "")
        assinatura_calculada = hmac.new(MERCADO_PAGO_WEBHOOK_SECRET, body_normalizado, hashlib.sha256).hexdigest()
        return hmac.compare_digest(assinatura_calculada, signature_value.strip())
    except Exception as e:
        logging.error(f"Erro ao validar assinatura: {e}")
        return False

def enviar_email_confirmacao_pagamento(destinatario):
    try:
        html = render_template('email/confirmacao_assinatura.html')
        msg = Message(subject="🎉 Assinatura Confirmada - TreinoRun", recipients=[destinatario], html=html)
        mail.send(msg)
        logging.info(f"E-mail de confirmação enviado para {destinatario}")
    except Exception as e:
        logging.error(f"Erro ao enviar e-mail de confirmação: {e}")

# ================================================
# ROTAS
# ================================================

@app.route("/webhook/mercadopago", methods=["GET", "POST"])
@limiter.limit("100 per day")
def mercadopago_webhook():
    try:
        raw_data = request.get_data()
        signature = request.headers.get("X-Signature", "")

        if not raw_data or raw_data.strip() in [b'', b'{}']:
            logging.warning("Webhook com body vazio")
            return jsonify({"error": "Empty body"}), 400

        if request.method == "GET":
            return jsonify({"status": "ok"}), 200

        payload = json.loads(raw_data.decode('utf-8'))

        if payload.get("id") == "123456":
            db.execute(text("INSERT INTO logs_webhook (data_recebimento, payload, status_processamento) VALUES (NOW(), :payload, 'teste')"), {"payload": json.dumps(payload)})
            db.commit()
            return jsonify({"status": "test notification received"}), 200

        if not validar_assinatura(raw_data, signature):
            return jsonify({"error": "Unauthorized"}), 401

        db.execute(text("INSERT INTO logs_webhook (data_recebimento, payload, status_processamento) VALUES (NOW(), :payload, 'recebido')"), {"payload": json.dumps(payload)})
        db.commit()

        if payload.get("type") == "subscription_preapproval":
            threading.Thread(target=processar_notificacao_assinatura, args=(payload,), daemon=True).start()

        return jsonify({"status": "received"}), 200

    except Exception as e:
        logging.error(f"Erro crítico webhook: {e}", exc_info=True)
        db.rollback()
        return jsonify({"error": "Internal server error"}), 500

def processar_notificacao_assinatura(payload):
    try:
        subscription_id = payload["data"]["id"]
        response = requests.get(f"https://api.mercadopago.com/preapproval/{subscription_id}", headers={"Authorization": f"Bearer {MERCADOPAGO_ACCESS_TOKEN}"})
        assinatura = response.json()

        email = assinatura.get("payer_email") or assinatura.get("external_reference")
        status = assinatura.get("status")

        if email and status:
            usuario = db.execute(text("SELECT id FROM usuarios WHERE email = :email"), {"email": email}).fetchone()
            if usuario:
                db.execute(text("UPDATE assinaturas SET status = :status, data_atualizacao = NOW() WHERE subscription_id = :subscription_id"), {"status": status, "subscription_id": subscription_id})
                db.commit()

                if status == "authorized":
                    enviar_email_confirmacao_pagamento(email)
    except Exception as e:
        logging.error(f"Erro ao processar assinatura: {e}")
        db.rollback()

# Outras rotas (landing, blog, etc.) aqui você já tem.

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))