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
# FUNÇÕES AUXILIARES (ATUALIZADAS)
# ================================================

def validar_assinatura(body, signature):
    chave_secreta = os.getenv("MERCADO_PAGO_WEBHOOK_SECRET").encode("utf-8")
    assinatura_esperada = hmac.new(chave_secreta, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(assinatura_esperada, signature)

def pode_gerar_plano(email, plano):
    # Verificação prioritária no banco de dados para usuários logados
    if session.get("logged_in"):
        try:
            usuario = db.execute(
                text("""
                    SELECT u.*, a.status as status_assinatura
                    FROM usuarios u
                    LEFT JOIN assinaturas a ON u.id = a.usuario_id
                    WHERE u.email = :email
                    ORDER BY a.id DESC LIMIT 1
                """),
                {"email": email}
            ).fetchone()

            if usuario:
                # Atualiza a sessão com os dados mais recentes
                session["assinatura_ativa"] = usuario.status_assinatura == "active"
                session["plano"] = usuario.plano
                
                if usuario.status_assinatura == "active":
                    return True  # Usuário premium pode gerar à vontade
                if plano == "gratuito":
                    return True  # Permite plano gratuito mesmo sem assinatura
                
        except Exception as e:
            logging.error(f"Erro ao verificar permissão: {str(e)}")
    
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
# ROTAS PRINCIPAIS (ATUALIZADAS)
# ================================================

@app.route("/")
def landing():
    email = session.get("email") or request.args.get("email")
    
    if email:
        # Busca dados atualizados do usuário
        usuario = db.execute(
            text("""
                SELECT u.*, a.status as status_assinatura 
                FROM usuarios u
                LEFT JOIN assinaturas a ON u.id = a.usuario_id
                WHERE u.email = :email
                ORDER BY a.id DESC LIMIT 1
            """), 
            {"email": email}
        ).fetchone()

        if usuario:
            session["logged_in"] = True
            session["email"] = email
            session["plano"] = usuario.plano
            session["assinatura_ativa"] = usuario.status_assinatura == "active"
            logging.info(f"Usuário {email} - Assinatura ativa: {usuario.status_assinatura == 'active'}")

    return render_template(
        "landing.html",
        logged_in=session.get("logged_in", False),
        email=session.get("email", ""),
        assinatura_ativa=session.get("assinatura_ativa", False)
    )

# ... (mantenha as outras rotas como estão, apenas atualize a rota /sucesso)

@app.route("/sucesso")
@limiter.limit("100 per hour")
def sucesso():
    try:
        email = session.get("email") or request.args.get("email")
        
        if email:
            usuario = db.execute(
                text("SELECT * FROM usuarios WHERE email = :email"),
                {"email": email}
            ).fetchone()

            if usuario:
                # Atualiza a sessão com os dados mais recentes
                session["logged_in"] = True
                session["email"] = usuario.email
                session["plano"] = usuario.plano
                
                # Verifica assinatura ativa
                assinatura = db.execute(
                    text("SELECT status FROM assinaturas WHERE usuario_id = :usuario_id ORDER BY id DESC LIMIT 1"),
                    {"usuario_id": usuario.id}
                ).fetchone()
                
                session["assinatura_ativa"] = assinatura and assinatura.status == "active"
                logging.info(f"Sucesso: Usuário {email} - Assinatura ativa: {session['assinatura_ativa']}")
            else:
                logging.warning(f"Usuário não encontrado para email: {email}")
        else:
            logging.warning("Nenhum email encontrado na sessão ou URL.")

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

# ... (mantenha as outras rotas como estão)

# ================================================
# ROTAS DE PAGAMENTO (ATUALIZADAS)
# ================================================

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
            
            # Atualiza a sessão se for o usuário atual
            if session.get("email") == email:
                session["assinatura_ativa"] = True
                session["plano"] = "anual"
                logging.info(f"Sessão atualizada para usuário {email} - Assinatura ativa")

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

# ... (mantenha o restante do código como está)

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