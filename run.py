from flask import Flask, request, render_template, redirect, url_for, session, jsonify
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker
from datetime import datetime, timedelta
import hmac
import hashlib
from openai import OpenAI
import requests
import json
from flask_mail import Mail, Message
from io import BytesIO
import base64
import logging
import re
from concurrent.futures import ThreadPoolExecutor

# ================================================
# CONFIGURAÇÕES INICIAIS
# ================================================

# Configuração do Flask
template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Templates")
app = Flask(__name__, template_folder=template_dir)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# Configuração do Banco de Dados PostgreSQL com pool otimizado
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(
    DATABASE_URL,
    pool_size=15,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600
)
db = scoped_session(sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False
))

# Configurações de API
MERCADO_PAGO_ACCESS_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN")
MERCADO_PAGO_WEBHOOK_SECRET = os.getenv("MERCADO_PAGO_WEBHOOK_SECRET")

# Configuração da OpenAI com timeout otimizado
client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=20.0,
    max_retries=2
)

# Configuração do Flask-Mail
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
    format='%(asctime)s %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Thread pool para operações assíncronas
executor = ThreadPoolExecutor(max_workers=10)

# ================================================
# FUNÇÕES AUXILIARES (OTIMIZADAS)
# ================================================

def validar_assinatura(body, signature):
    """Valida a assinatura do webhook do Mercado Pago de forma eficiente"""
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
    """Verifica assincronamente se o usuário pode gerar um novo plano"""
    try:
        usuario = await asyncio.get_event_loop().run_in_executor(
            executor,
            lambda: db.execute(
                text("SELECT * FROM usuarios WHERE email = :email"),
                {"email": email}
            ).mappings().fetchone()
        )

        if plano == "anual":
            if usuario:
                assinatura = await asyncio.get_event_loop().run_in_executor(
                    executor,
                    lambda: db.execute(
                        text("SELECT status FROM assinaturas WHERE usuario_id = :usuario_id ORDER BY id DESC LIMIT 1"),
                        {"usuario_id": usuario["id"]}
                    ).mappings().fetchone()
                )
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

def calcular_semanas(tempo_melhoria):
    """Calcula o número de semanas de forma otimizada"""
    try:
        tempo_melhoria = tempo_melhoria.lower().replace("meses", "mês").replace("mese", "mês")
        
        match = re.search(r'(\d+)\s*(semanas?|meses?|mês|m)', tempo_melhoria)
        
        if not match:
            return 4
        
        valor = int(match.group(1))
        unidade = match.group(2)
        
        if 'semana' in unidade:
            return min(max(valor, 2), 52)
        elif 'mes' in unidade or 'mês' in unidade or 'm' in unidade:
            return min(max(valor * 4, 4), 52)
        return 4
    except Exception as e:
        logger.error(f"Erro ao calcular semanas: {e}")
        return 4

async def registrar_geracao(email, plano):
    """Registra a geração de forma assíncrona"""
    try:
        hoje = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        usuario = await asyncio.get_event_loop().run_in_executor(
            executor,
            lambda: db.execute(
                text("SELECT * FROM usuarios WHERE email = :email"),
                {"email": email}
            ).mappings().fetchone()
        )

        if not usuario:
            await asyncio.get_event_loop().run_in_executor(
                executor,
                lambda: db.execute(
                    text("""
                        INSERT INTO usuarios (email, plano, data_inscricao, ultima_geracao)
                        VALUES (:email, :plano, :data_inscricao, :ultima_geracao)
                    """),
                    {"email": email, "plano": plano, "data_inscricao": hoje, "ultima_geracao": hoje},
                )
            )
            usuario_id = await asyncio.get_event_loop().run_in_executor(
                executor,
                lambda: db.execute(text("SELECT id FROM usuarios WHERE email = :email"), {"email": email}).fetchone()[0]
            )
        else:
            await asyncio.get_event_loop().run_in_executor(
                executor,
                lambda: db.execute(
                    text("UPDATE usuarios SET ultima_geracao = :ultima_geracao, plano = :plano WHERE email = :email"),
                    {"ultima_geracao": hoje, "email": email, "plano": plano},
                )
            )
            usuario_id = usuario["id"]

        await asyncio.get_event_loop().run_in_executor(
            executor,
            lambda: db.execute(
                text("INSERT INTO geracoes (usuario_id, data_geracao) VALUES (:usuario_id, :data_geracao)"),
                {"usuario_id": usuario_id, "data_geracao": hoje},
            )
        )
        db.commit()
    except Exception as e:
        logger.error(f"Erro ao registrar geração: {e}")
        db.rollback()

async def gerar_plano_openai(prompt, semanas):
    """Gera o plano de forma assíncrona com timeout"""
    try:
        model = "gpt-3.5-turbo-16k" if semanas > 12 else "gpt-3.5-turbo"
        
        prompt_enhanced = f"""{prompt}
        
        REGRAS ABSOLUTAS:
        1. Detalhe todas as {semanas} semanas individualmente
        2. A última semana deve atingir o objetivo final
        3. Progressão realista semana a semana
        4. Nunca mencione "resposta separada" ou similar
        5. Formato consistente para todas semanas
        """

        response = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                executor,
                lambda: client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "Você é um treinador de corrida experiente. Siga exatamente as instruções."},
                        {"role": "user", "content": prompt_enhanced},
                    ],
                    temperature=0.7,
                )
            ),
            timeout=25.0
        )

        plano = response.choices[0].message.content.strip()
        
        # Verificação rápida de semanas faltantes
        if semanas <= 12 and plano.lower().count("semana") < semanas:
            complemento = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    executor,
                    lambda: client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": "Complete o plano com as semanas faltantes."},
                            {"role": "user", "content": f"Complete as semanas {plano.lower().count('semana')+1} a {semanas}:\n{plano}"},
                        ],
                        max_tokens=1500,
                        temperature=0.7,
                    )
                ),
                timeout=15.0
            )
            plano += "\n\n" + complemento.choices[0].message.content.strip()
        
        # Limpeza rápida do resultado
        plano = re.sub(r"(próximas? semanas? (serão|em) (outra|uma) resposta)", "", plano, flags=re.IGNORECASE)
        return plano
        
    except asyncio.TimeoutError:
        logger.error("Timeout ao gerar plano com OpenAI")
        return "Erro: tempo de resposta excedido. Tente novamente."
    except Exception as e:
        logger.error(f"Erro ao gerar plano: {e}")
        return "Erro ao gerar o plano. Tente novamente mais tarde."

# ================================================
# ROTAS PRINCIPAIS (OTIMIZADAS)
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
    titulo = session.get("titulo", "Plano de Treino")
    plano = session.get("plano", "Nenhum plano gerado.")
    return render_template("resultado.html", titulo=titulo, plano=plano)

# ================================================
# ROTAS DE GERACAO DE PLANOS (OTIMIZADAS)
# ================================================

@app.route("/generate", methods=["POST"])
async def generate():
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
        
        # Validação rápida de objetivos
        objetivo = dados_usuario['objetivo'].lower()
        if "maratona" in objetivo and semanas < 16:
            return "Mínimo 16 semanas para maratona", 400
        elif "meia-maratona" in objetivo and semanas < 12:
            return "Mínimo 12 semanas para meia-maratona", 400

        prompt = f"""
        Crie um plano de corrida para {dados_usuario['objetivo']} em {dados_usuario['tempo_melhoria']},
        {dados_usuario['dias']} dias/semana, sessões de {dados_usuario['tempo']} minutos.

        NÍVEL: {dados_usuario['nivel']}
        SEMANAS: {semanas}
        OBJETIVO FINAL: Atingir {dados_usuario['objetivo']} na semana {semanas}

        FORMATO EXIGIDO:
        - Semana X: [Objetivo específico]
          - Dia 1: [Tipo], [Tempo], [Detalhes]
          - Dia 2: [Tipo], [Tempo], [Detalhes]
          - [...]
        """

        plano_gerado = await gerar_plano_openai(prompt, semanas)
        
        # Registrar em segundo plano sem bloquear a resposta
        asyncio.create_task(registrar_geracao(email, plano))

        session["titulo"] = "Plano de Corrida"
        session["plano"] = DISCLAIMER + plano_gerado
        return redirect(url_for("resultado"))

    except Exception as e:
        logger.error(f"Erro na geração: {e}")
        return "Erro interno", 500

@app.route("/generatePace", methods=["POST"])
async def generatePace():
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
            partes = dados_usuario['objetivo'].split("para")
            pace_inicial = float(partes[0].strip())
            pace_final = float(partes[1].strip())
            
            if (pace_inicial - pace_final)/semanas > 0.5:
                return "Progressão muito rápida", 400
        except:
            pass

        prompt = f"""
        Crie um plano para melhorar o pace de {dados_usuario['objetivo']} em {dados_usuario['tempo_melhoria']},
        {dados_usuario['dias']} dias/semana, sessões de {dados_usuario['tempo']} minutos.

        NÍVEL: {dados_usuario['nivel']}
        SEMANAS: {semanas}
        OBJETIVO FINAL: Pace de {pace_final} min/km na semana {semanas}

        FORMATO EXIGIDO:
        - Semana X: [Pace alvo]
          - Dia 1: [Tipo], [Pace], [Detalhes]
          - Dia 2: [Tipo], [Pace], [Detalhes]
          - [...]
        """

        plano_gerado = await gerar_plano_openai(prompt, semanas)
        
        # Registrar em segundo plano
        asyncio.create_task(registrar_geracao(email, plano))

        session["titulo"] = "Plano de Pace"
        session["plano"] = DISCLAIMER + plano_gerado
        return redirect(url_for("resultado"))

    except Exception as e:
        logger.error(f"Erro na geração de pace: {e}")
        return "Erro interno", 500

# ================================================
# ROTAS DE PAGAMENTO (OTIMIZADAS)
# ================================================

@app.route("/iniciar_pagamento", methods=["GET", "POST"])
def iniciar_pagamento():
    try:
        dados_usuario = request.form if request.method == "POST" else request.args
        email = dados_usuario.get("email")
        
        if not email:
            return "Email necessário", 400

        # Registrar tentativa em segundo plano
        executor.submit(
            lambda: db.execute(
                text("INSERT INTO tentativas_pagamento (email, data_tentativa) VALUES (:email, NOW())"),
                {"email": email}
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
                "Authorization": f"Bearer {MERCADO_PAGO_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=MERCADO_PAGO_TIMEOUT
        )
        response.raise_for_status()
        return redirect(response.json()["init_point"])
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Erro Mercado Pago: {e}")
        return "Erro no pagamento", 500
    except Exception as e:
        logger.error(f"Erro inesperado: {e}")
        return "Erro interno", 500

# ================================================
# WEBHOOK E EMAIL (OTIMIZADOS)
# ================================================

@app.route("/webhook/mercadopago", methods=["POST"])
def mercadopago_webhook():
    try:
        payload = request.json
        signature = request.headers.get("X-Signature")
        
        # Registrar log em segundo plano
        executor.submit(
            lambda: db.execute(
                text("INSERT INTO logs_webhook (payload, status_processamento) VALUES (:payload, 'recebido')"),
                {"payload": json.dumps(payload)}
            )
        )

        if not validar_assinatura(request.get_data(), signature):
            raise ValueError("Assinatura inválida")

        evento = payload.get("action")
        
        if evento == "payment.updated":
            payment_id = payload.get("data", {}).get("id")
            status = payload.get("data", {}).get("status")
            
            executor.submit(
                lambda: db.execute(
                    text("""
                        INSERT INTO pagamentos (payment_id, status, data_pagamento)
                        VALUES (:payment_id, :status, NOW())
                        ON CONFLICT (payment_id) DO NOTHING
                    """),
                    {"payment_id": payment_id, "status": status}
                )
            )

        elif evento == "subscription.updated":
            subscription_id = payload.get("data", {}).get("id")
            status = payload.get("data", {}).get("status")
            email = payload.get("data", {}).get("payer", {}).get("email")
            nome = payload.get("data", {}).get("payer", {}).get("first_name", "Cliente")

            if email:
                def processar_assinatura():
                    usuario = db.execute(
                        text("SELECT id FROM usuarios WHERE email = :email"),
                        {"email": email}
                    ).fetchone()

                    if not usuario:
                        db.execute(
                            text("INSERT INTO usuarios (email, data_inscricao) VALUES (:email, NOW())"),
                            {"email": email}
                        )
                        usuario_id = db.execute(
                            text("SELECT id FROM usuarios WHERE email = :email"),
                            {"email": email}
                        ).fetchone()[0]
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
                        executor.submit(enviar_email_confirmacao_pagamento, email, nome)

                executor.submit(processar_assinatura)

        return jsonify({"status": "success"}), 200

    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/send_plan_email', methods=['POST'])
def send_plan_email():
    try:
        if not request.is_json:
            return jsonify({"success": False, "message": "JSON required"}), 400

        data = request.get_json()
        recipient = data.get('email')
        pdf_data = data.get('pdfData')
        
        if not recipient or not pdf_data:
            return jsonify({"success": False, "message": "Dados incompletos"}), 400

        tipo_treino = session.get("titulo", "Plano de Treino")
        nome_arquivo = f"TREINO_{tipo_treino.upper().replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.pdf"

        # Enviar email em segundo plano
        executor.submit(
            lambda: _enviar_email_plano(recipient, tipo_treino, nome_arquivo, pdf_data)
        )

        return jsonify({"success": True, "message": "E-mail em processamento"})

    except Exception as e:
        logger.error(f"Erro ao enviar e-mail: {e}")
        return jsonify({"success": False, "message": "Erro interno"}), 500

def _enviar_email_plano(recipient, tipo_treino, nome_arquivo, pdf_data):
    """Função auxiliar para envio de email em background"""
    try:
        msg = Message(
            subject=f"Seu {tipo_treino} - TreinoRun",
            recipients=[recipient],
            html=f"""
            <!DOCTYPE html>
            <html>
            <body>
                <h2>Seu {tipo_treino} Personalizado</h2>
                <p>Segue em anexo o seu plano de treino.</p>
                <p>Atenciosamente,<br>Equipe TreinoRun</p>
            </body>
            </html>
            """
        )

        pdf_content = base64.b64decode(pdf_data.split(',')[1])
        msg.attach(nome_arquivo, "application/pdf", pdf_content)
        mail.send(msg)
        logger.info(f"E-mail enviado para: {recipient}")
    except Exception as e:
        logger.error(f"Falha ao enviar e-mail: {e}")

# ================================================
# INICIALIZAÇÃO
# ================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)