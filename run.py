from flask import Flask, request, render_template, redirect, url_for, session, jsonify
from flask.helpers import ensure_sync
import os
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timedelta
import hmac
import hashlib
from openai import AsyncOpenAI
import aiohttp
import json
from flask_mail import Mail, Message
from io import BytesIO
import base64
import logging
import re
import asyncio
from concurrent.futures import ThreadPoolExecutor

# ================================================
# CONFIGURAÇÕES INICIAIS
# ================================================

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# Configuração do Banco de Dados PostgreSQL Async
DATABASE_URL = os.getenv("DATABASE_URL").replace("postgresql://", "postgresql+asyncpg://")
engine = create_async_engine(
    DATABASE_URL,
    pool_size=20,
    max_overflow=30,
    pool_pre_ping=True,
    pool_recycle=3600
)
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# Configurações de API
MERCADO_PAGO_ACCESS_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN")
MERCADO_PAGO_WEBHOOK_SECRET = os.getenv("MERCADO_PAGO_WEBHOOK_SECRET")

# Configuração da OpenAI Async
client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=30.0,
    max_retries=3
)

# Configuração do Flask-Mail (síncrono)
app.config['MAIL_SERVER'] = 'smtp.zoho.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('ZOHO_EMAIL')
app.config['MAIL_PASSWORD'] = os.getenv('ZOHO_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('ZOHO_EMAIL')
mail = Mail(app)

# Variáveis globais
DISCLAIMER = "Este plano é gerado automaticamente. Consulte um profissional para ajustes personalizados.\n\n"

# Configuração de logging para produção
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s [%(filename)s:%(lineno)d]',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Thread pool para operações síncronas
executor = ThreadPoolExecutor(max_workers=10)

# ================================================
# FUNÇÕES AUXILIARES (ASYNC)
# ================================================

async def get_db_session():
    """Retorna uma sessão assíncrona do banco de dados"""
    async with AsyncSessionLocal() as session:
        yield session

async def validar_assinatura(body, signature):
    """Valida a assinatura do webhook do Mercado Pago"""
    if not MERCADO_PAGO_WEBHOOK_SECRET:
        logger.error("Assinatura secreta do webhook não configurada")
        return False
    
    try:
        chave_secreta = MERCADO_PAGO_WEBHOOK_SECRET.encode()
        assinatura_esperada = hmac.new(chave_secreta, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(assinatura_esperada, signature)
    except Exception as e:
        logger.error(f"Erro ao validar assinatura: {e}")
        return False

async def pode_gerar_plano(email, plano):
    """Verifica se o usuário pode gerar um novo plano"""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT * FROM usuarios WHERE email = :email"),
                {"email": email}
            )
            usuario = result.mappings().first()

            if plano == "anual":
                if usuario:
                    result = await session.execute(
                        text("""
                            SELECT status FROM assinaturas 
                            WHERE usuario_id = :usuario_id 
                            ORDER BY id DESC LIMIT 1
                        """),
                        {"usuario_id": usuario["id"]}
                    )
                    assinatura = result.mappings().first()
                    return assinatura and assinatura["status"] == "active"
                return False

            elif plano == "gratuito":
                if usuario and usuario["ultima_geracao"]:
                    ultima_geracao = datetime.strptime(str(usuario["ultima_geracao"]), "%Y-%m-%d %H:%M:%S")
                    return (datetime.now() - ultima_geracao).days >= 30
                return True

            return False
    except Exception as e:
        logger.error(f"Erro ao verificar permissão de geração: {e}")
        return False

async def registrar_geracao(email, plano):
    """Registra a geração de um novo plano no banco de dados"""
    try:
        hoje = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT * FROM usuarios WHERE email = :email"),
                {"email": email}
            )
            usuario = result.mappings().first()

            if not usuario:
                await session.execute(
                    text("""
                        INSERT INTO usuarios (email, plano, data_inscricao, ultima_geracao)
                        VALUES (:email, :plano, :data_inscricao, :ultima_geracao)
                    """),
                    {"email": email, "plano": plano, "data_inscricao": hoje, "ultima_geracao": hoje},
                )
                await session.commit()
                
                result = await session.execute(
                    text("SELECT id FROM usuarios WHERE email = :email"),
                    {"email": email}
                )
                usuario_id = result.scalar_one()
            else:
                await session.execute(
                    text("""
                        UPDATE usuarios 
                        SET ultima_geracao = :ultima_geracao, plano = :plano 
                        WHERE email = :email
                    """),
                    {"ultima_geracao": hoje, "email": email, "plano": plano},
                )
                await session.commit()
                usuario_id = usuario["id"]

            await session.execute(
                text("""
                    INSERT INTO geracoes (usuario_id, data_geracao)
                    VALUES (:usuario_id, :data_geracao)
                """),
                {"usuario_id": usuario_id, "data_geracao": hoje},
            )
            await session.commit()
    except Exception as e:
        logger.error(f"Erro ao registrar geração: {e}")
        if 'session' in locals():
            await session.rollback()

async def gerar_plano_openai(prompt, semanas):
    """Gera o plano de treino usando a API da OpenAI"""
    try:
        model = "gpt-3.5-turbo-16k" if semanas > 12 else "gpt-3.5-turbo"
        
        prompt_enhanced = f"""{prompt}
        
        REGRAS ABSOLUTAS:
        1. Detalhe todas as {semanas} semanas individualmente
        2. A última semana deve atingir o objetivo final
        3. Progressão realista semana a semana
        4. Formato consistente para todas semanas
        5. Nunca mencione "continua" ou "próxima parte"
        """

        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "Você é um treinador de corrida experiente. Siga exatamente as instruções."
                },
                {
                    "role": "user", 
                    "content": prompt_enhanced
                },
            ],
            temperature=0.7,
            timeout=30.0
        )

        plano = response.choices[0].message.content.strip()
        
        # Verificação de semanas faltantes
        if semanas <= 12 and plano.lower().count("semana") < semanas:
            complemento = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "Complete o plano com as semanas faltantes."
                    },
                    {
                        "role": "user",
                        "content": f"Complete as semanas {plano.lower().count('semana')+1} a {semanas}:\n{plano}"
                    },
                ],
                temperature=0.7,
                timeout=20.0
            )
            plano += "\n\n" + complemento.choices[0].message.content.strip()
        
        # Limpeza do resultado
        plano = re.sub(r"(próximas? semanas? (serão|em) (outra|uma) resposta)", "", plano, flags=re.IGNORECASE)
        return plano
        
    except Exception as e:
        logger.error(f"Erro ao gerar plano: {e}")
        return "Erro ao gerar o plano. Por favor, tente novamente."

# ================================================
# ROTAS PRINCIPAIS (WRAPPERS ASYNC)
# ================================================

@app.route("/")
def landing():
    return render_template("landing.html")

@app.route("/seutreino")
def seutreino():
    return render_template("seutreino.html")

@app.route("/sucesso")
def sucesso():
    return render_template("sucesso.html")

@app.route("/erro")
def erro():
    return render_template("erro.html")

@app.route("/pendente")
def pendente():
    return render_template("pendente.html")

@app.route("/resultado")
def resultado():
    if "plano" not in session:
        return redirect(url_for("seutreino"))
    return render_template(
        "resultado.html",
        titulo=session.get("titulo", "Plano de Treino"),
        plano=session.get("plano")
    )

# ================================================
# ROTAS ASSÍNCRONAS PRINCIPAIS
# ================================================

@app.route("/generate", methods=["POST"])
def generate():
    return ensure_sync(_generate)()

async def _generate():
    try:
        dados_usuario = request.form
        required_fields = ["email", "plano", "objetivo", "tempo_melhoria", "nivel", "dias", "tempo"]
        
        if not all(field in dados_usuario for field in required_fields):
            logger.error("Campos obrigatórios faltando")
            return "Dados incompletos", 400

        email = dados_usuario["email"]
        plano = dados_usuario["plano"]

        if not await pode_gerar_plano(email, plano):
            if plano == "anual":
                return redirect(url_for("iniciar_pagamento", email=email))
            return redirect(url_for("seutreino", alerta="limite_atingido"))

        semanas = calcular_semanas(dados_usuario['tempo_melhoria'])
        
        # Validação de objetivos
        objetivo = dados_usuario['objetivo'].lower()
        if "maratona" in objetivo and semanas < 16:
            return redirect(url_for("seutreino", alerta="tempo_insuficiente_maratona"))
        elif "meia-maratona" in objetivo and semanas < 12:
            return redirect(url_for("seutreino", alerta="tempo_insuficiente_meia"))

        prompt = f"""
        Crie um plano de corrida para {dados_usuario['objetivo']} em {dados_usuario['tempo_melhoria']},
        {dados_usuario['dias']} dias/semana, sessões de {dados_usuario['tempo']} minutos.

        NÍVEL: {dados_usuario['nivel']}
        SEMANAS: {semanas}
        OBJETIVO FINAL: Atingir {dados_usuario['objetivo']} na semana {semanas}

        INSTRUÇÕES:
        1. Progressão semanal realista
        2. Detalhar todos os dias de treino
        3. Incluir aquecimento e desaquecimento
        4. Variar intensidade conforme nível
        """

        plano_gerado = await gerar_plano_openai(prompt, semanas)
        
        if "Erro ao gerar" in plano_gerado:
            return redirect(url_for("seutreino", alerta="erro_geracao"))

        # Registrar em segundo plano
        asyncio.create_task(registrar_geracao(email, plano))

        session["titulo"] = "Plano de Corrida"
        session["plano"] = DISCLAIMER + plano_gerado
        return redirect(url_for("resultado"))

    except Exception as e:
        logger.error(f"Erro não tratado: {e}")
        return redirect(url_for("erro"))

@app.route("/generatePace", methods=["POST"])
def generatePace():
    return ensure_sync(_generatePace)()

async def _generatePace():
    try:
        dados_usuario = request.form
        required_fields = ["email", "plano", "objetivo", "tempo_melhoria", "nivel", "dias", "tempo"]
        
        if not all(field in dados_usuario for field in required_fields):
            return "Dados incompletos", 400

        email = dados_usuario["email"]
        plano = dados_usuario["plano"]

        if not await pode_gerar_plano(email, plano):
            if plano == "anual":
                return redirect(url_for("iniciar_pagamento", email=email))
            return "Limite de geração atingido", 400

        semanas = calcular_semanas(dados_usuario['tempo_melhoria'])
        
        # Parse do objetivo de pace
        try:
            partes = dados_usuario['objetivo'].lower().split("para")
            pace_inicial = float(partes[0].strip())
            pace_final = float(partes[1].strip())
            
            if (pace_inicial - pace_final)/semanas > 0.5:
                return redirect(url_for("seutreino", alerta="progressao_rapida"))
        except Exception as e:
            logger.error(f"Erro ao parsear pace: {e}")
            return redirect(url_for("seutreino", alerta="formato_invalido"))

        prompt = f"""
        Crie um plano para melhorar o pace de {dados_usuario['objetivo']} em {dados_usuario['tempo_melhoria']},
        {dados_usuario['dias']} dias/semana, sessões de {dados_usuario['tempo']} minutos.

        NÍVEL: {dados_usuario['nivel']}
        SEMANAS: {semanas}
        PACE INICIAL: {pace_inicial} min/km
        PACE FINAL: {pace_final} min/km (atingir na semana {semanas})

        INSTRUÇÕES:
        1. Redução progressiva do pace
        2. Incluir treinos intervalados e longos
        3. Semana final deve atingir pace objetivo
        4. Detalhar todos os treinos
        """

        plano_gerado = await gerar_plano_openai(prompt, semanas)
        
        if "Erro ao gerar" in plano_gerado:
            return redirect(url_for("seutreino", alerta="erro_geracao"))

        asyncio.create_task(registrar_geracao(email, plano))

        session["titulo"] = "Plano de Pace"
        session["plano"] = DISCLAIMER + plano_gerado
        return redirect(url_for("resultado"))

    except Exception as e:
        logger.error(f"Erro não tratado: {e}")
        return redirect(url_for("erro"))

# ================================================
# ROTAS DE PAGAMENTO (SÍNCRONAS COM TASKS ASYNC)
# ================================================

@app.route("/iniciar_pagamento", methods=["GET", "POST"])
def iniciar_pagamento():
    try:
        dados_usuario = request.form if request.method == "POST" else request.args
        email = dados_usuario.get("email")
        
        if not email or not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            return "Email inválido", 400

        # Registrar tentativa em segundo plano
        executor.submit(
            lambda: asyncio.run(
                registrar_tentativa_pagamento(email)
            )
        )

        payload = {
            "items": [{
                "title": "Plano Anual de Treino",
                "quantity": 1,
                "unit_price": 59.9,
                "currency_id": "BRL",
            }],
            "payer": {"email": email},
            "back_urls": {
                "success": f"{os.getenv('BASE_URL')}/sucesso",
                "failure": f"{os.getenv('BASE_URL')}/erro",
                "pending": f"{os.getenv('BASE_URL')}/pendente",
            },
            "auto_return": "approved",
            "notification_url": f"{os.getenv('BASE_URL')}/webhook/mercadopago",
        }

        async def criar_preferencia():
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.mercadopago.com/checkout/preferences",
                    headers={
                        "Authorization": f"Bearer {MERCADO_PAGO_ACCESS_TOKEN}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=10
                ) as response:
                    response.raise_for_status()
                    return await response.json()

        preferencia = asyncio.run(criar_preferencia())
        return redirect(preferencia["init_point"])
        
    except Exception as e:
        logger.error(f"Erro no pagamento: {e}")
        return redirect(url_for("erro"))

async def registrar_tentativa_pagamento(email):
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO tentativas_pagamento (email, data_tentativa)
                    VALUES (:email, NOW())
                """),
                {"email": email}
            )
            await session.commit()
    except Exception as e:
        logger.error(f"Erro ao registrar tentativa: {e}")

# ================================================
# WEBHOOK E EMAIL (OTIMIZADOS)
# ================================================

@app.route("/webhook/mercadopago", methods=["POST"])
def mercadopago_webhook():
    return ensure_sync(_mercadopago_webhook)()

async def _mercadopago_webhook():
    try:
        payload = request.json
        signature = request.headers.get("X-Signature")
        
        # Registrar log em segundo plano
        asyncio.create_task(
            registrar_log_webhook(payload)
        )

        if not await validar_assinatura(request.get_data(), signature):
            logger.warning("Assinatura inválida recebida")
            return jsonify({"error": "Assinatura inválida"}), 401

        evento = payload.get("action")
        
        if evento == "payment.updated":
            payment_id = payload.get("data", {}).get("id")
            status = payload.get("data", {}).get("status")
            
            asyncio.create_task(
                registrar_pagamento(payment_id, status)
            )

        elif evento == "subscription.updated":
            subscription_id = payload.get("data", {}).get("id")
            status = payload.get("data", {}).get("status")
            email = payload.get("data", {}).get("payer", {}).get("email")
            nome = payload.get("data", {}).get("payer", {}).get("first_name", "Cliente")

            if email:
                asyncio.create_task(
                    processar_assinatura(subscription_id, email, status, nome)
                )

        return jsonify({"status": "success"}), 200

    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        return jsonify({"error": "Erro interno"}), 500

async def registrar_log_webhook(payload):
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO logs_webhook (payload, status_processamento)
                    VALUES (:payload, 'recebido')
                """),
                {"payload": json.dumps(payload)}
            )
            await session.commit()
    except Exception as e:
        logger.error(f"Erro ao registrar log: {e}")

async def registrar_pagamento(payment_id, status):
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO pagamentos (payment_id, status, data_pagamento)
                    VALUES (:payment_id, :status, NOW())
                    ON CONFLICT (payment_id) DO NOTHING
                """),
                {"payment_id": payment_id, "status": status}
            )
            await session.commit()
    except Exception as e:
        logger.error(f"Erro ao registrar pagamento: {e}")

async def processar_assinatura(subscription_id, email, status, nome):
    try:
        async with AsyncSessionLocal() as session:
            # Verificar/inserir usuário
            result = await session.execute(
                text("SELECT id FROM usuarios WHERE email = :email"),
                {"email": email}
            )
            usuario = result.scalar()
            
            if not usuario:
                await session.execute(
                    text("""
                        INSERT INTO usuarios (email, data_inscricao)
                        VALUES (:email, NOW())
                    """),
                    {"email": email}
                )
                await session.commit()
                
                result = await session.execute(
                    text("SELECT id FROM usuarios WHERE email = :email"),
                    {"email": email}
                )
                usuario_id = result.scalar_one()
            else:
                usuario_id = usuario

            # Atualizar assinatura
            await session.execute(
                text("""
                    INSERT INTO assinaturas (subscription_id, usuario_id, status, data_atualizacao)
                    VALUES (:subscription_id, :usuario_id, :status, NOW())
                    ON CONFLICT (subscription_id) DO UPDATE
                    SET status = EXCLUDED.status, data_atualizacao = NOW()
                """),
                {
                    "subscription_id": subscription_id,
                    "usuario_id": usuario_id,
                    "status": status
                }
            )
            await session.commit()

            # Enviar e-mail se ativo
            if status == "active":
                executor.submit(
                    enviar_email_confirmacao_pagamento,
                    email,
                    nome
                )
    except Exception as e:
        logger.error(f"Erro ao processar assinatura: {e}")

def enviar_email_confirmacao_pagamento(email, nome):
    try:
        msg = Message(
            subject="Confirmação de Pagamento - Plano Anual",
            recipients=[email],
            html=f"""
            <!DOCTYPE html>
            <html>
            <body>
                <h2>Olá {nome},</h2>
                <p>Seu pagamento foi confirmado com sucesso!</p>
                <p>Agora você pode gerar planos ilimitados por 1 ano.</p>
                <p>Acesse: <a href="{os.getenv('BASE_URL')}/seutreino">Gerar Planos</a></p>
            </body>
            </html>
            """
        )
        mail.send(msg)
        logger.info(f"E-mail enviado para {email}")
    except Exception as e:
        logger.error(f"Erro ao enviar e-mail: {e}")

@app.route('/send_plan_email', methods=['POST'])
def send_plan_email():
    return ensure_sync(_send_plan_email)()

async def _send_plan_email():
    try:
        if not request.is_json:
            return jsonify({"success": False, "message": "Requisição deve ser JSON"}), 400

        data = request.get_json()
        recipient = data.get('email')
        pdf_data = data.get('pdfData')
        
        if not recipient or not pdf_data:
            return jsonify({"success": False, "message": "Dados incompletos"}), 400

        # Validar e-mail
        if not re.match(r"[^@]+@[^@]+\.[^@]+", recipient):
            return jsonify({"success": False, "message": "E-mail inválido"}), 400

        tipo_treino = session.get("titulo", "Plano de Treino")
        nome_arquivo = f"TREINO_{tipo_treino.upper().replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.pdf"

        # Enviar em segundo plano
        executor.submit(
            enviar_email_com_anexo,
            recipient,
            tipo_treino,
            nome_arquivo,
            pdf_data
        )

        return jsonify({"success": True, "message": "E-mail sendo processado"})

    except Exception as e:
        logger.error(f"Erro ao enviar e-mail: {e}")
        return jsonify({"success": False, "message": "Erro interno"}), 500

def enviar_email_com_anexo(recipient, tipo_treino, nome_arquivo, pdf_data):
    try:
        msg = Message(
            subject=f"Seu {tipo_treino} - TreinoRun",
            recipients=[recipient],
            html=f"""
            <!DOCTYPE html>
            <html>
            <body>
                <h2>Seu {tipo_treino} Personalizado</h2>
                <p>Segue em anexo o seu plano de treino gerado.</p>
                <p>Atenciosamente,<br>Equipe TreinoRun</p>
            </body>
            </html>
            """
        )

        pdf_content = base64.b64decode(pdf_data.split(',')[1])
        msg.attach(nome_arquivo, "application/pdf", pdf_content)
        mail.send(msg)
        logger.info(f"E-mail com anexo enviado para {recipient}")
    except Exception as e:
        logger.error(f"Falha ao enviar e-mail para {recipient}: {e}")

# ================================================
# INICIALIZAÇÃO
# ================================================

if __name__ == "__main__":
    from hypercorn.config import Config
    from hypercorn.asyncio import serve
    
    config = Config()
    config.bind = ["0.0.0.0:8080"]
    config.worker_class = "asyncio"
    config.workers = 2  # Ajuste conforme núcleos de CPU
    
    asyncio.run(serve(app, config))