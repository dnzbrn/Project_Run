from flask import Flask, request, render_template, redirect, url_for, session, jsonify
import asyncio
from functools import wraps
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

# ================================================
# CONFIGURAÇÃO INICIAL
# ================================================

basedir = os.path.abspath(os.path.dirname(__file__))
template_dir = os.path.join(basedir, 'Templates')

app = Flask(__name__, template_folder=template_dir)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "uma_chave_segura_aqui")

# Decorator para rotas assíncronas
def async_route(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrapper

# ================================================
# CONFIGURAÇÕES DO BANCO DE DADOS E APIs
# ================================================

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)
db = scoped_session(sessionmaker(bind=engine))

MERCADO_PAGO_ACCESS_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN")
MERCADO_PAGO_WEBHOOK_SECRET = os.getenv("MERCADO_PAGO_WEBHOOK_SECRET")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app.config['MAIL_SERVER'] = 'smtp.zoho.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('ZOHO_EMAIL')
app.config['MAIL_PASSWORD'] = os.getenv('ZOHO_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('ZOHO_EMAIL')
mail = Mail(app)

DISCLAIMER = "Este plano é gerado automaticamente. Consulte um profissional para ajustes personalizados.\n\n"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================================================
# FUNÇÕES AUXILIARES
# ================================================

def validar_assinatura(body, signature):
    if not MERCADO_PAGO_WEBHOOK_SECRET:
        raise ValueError("Assinatura secreta do webhook não configurada.")
    chave_secreta = MERCADO_PAGO_WEBHOOK_SECRET.encode("utf-8")
    assinatura_esperada = hmac.new(chave_secreta, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(assinatura_esperada, signature)

def pode_gerar_plano(email, plano):
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
        usuario_id = db.execute(text("SELECT id FROM usuarios WHERE email = :email"), {"email": email}).fetchone()[0]
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
        logger.error(f"Erro ao gerar plano com OpenAI: {e}")
        return "Erro ao gerar o plano. Tente novamente mais tarde."

def enviar_email_confirmacao_pagamento(email, nome="Cliente"):
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
                <p>Acesse: <a href="https://treinorun.com.br/seutreino">Gerar Planos</a></p>
            </body>
            </html>
            """
        )
        mail.send(msg)
        logger.info(f"E-mail enviado para {email}")
    except Exception as e:
        logger.error(f"Erro ao enviar e-mail: {e}")

# ================================================
# ROTAS PRINCIPAIS
# ================================================

@app.route("/")
def landing():
    try:
        return render_template("landing.html")
    except Exception as e:
        logger.error(f"Erro ao renderizar landing.html: {e}")
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

@app.route("/seutreino")
def seutreino():
    try:
        return render_template("seutreino.html")
    except Exception as e:
        logger.error(f"Erro ao renderizar seutreino.html: {e}")
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
def sucesso():
    try:
        return render_template("sucesso.html")
    except Exception as e:
        logger.error(f"Erro ao renderizar sucesso.html: {e}")
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
def erro():
    try:
        return render_template("erro.html")
    except Exception as e:
        logger.error(f"Erro ao renderizar erro.html: {e}")
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
def pendente():
    try:
        return render_template("pendente.html")
    except Exception as e:
        logger.error(f"Erro ao renderizar pendente.html: {e}")
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
def resultado():
    try:
        titulo = session.get("titulo", "Plano de Treino")
        plano = session.get("plano", "Nenhum plano gerado.")
        return render_template("resultado.html", titulo=titulo, plano=plano)
    except Exception as e:
        logger.error(f"Erro ao renderizar resultado.html: {e}")
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
    
    2. ESTRUTURA DIÁRIA:
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
    registrar_geracao(email, plano)

    session["titulo"] = f"Plano para: {dados_usuario['objetivo']}"
    session["plano"] = DISCLAIMER + plano_gerado
    return redirect(url_for("resultado"))

@app.route("/generatePace", methods=["POST"])
@async_route
async def generatePace():
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
    
    try:
        objetivo = dados_usuario['objetivo'].lower()
        
        # Padrões flexíveis para extração de paces
        padrao1 = r'(\d+[\.,]?\d*)\s*(?:min(?:uto)?s?\/km|min\/km|min km|pace|ritmo)[^\d]*(\d+[\.,]?\d*)'
        padrao2 = r'(?:reduzir|melhorar|diminuir|baixar).*?(\d+[\.,]?\d*).*?para.*?(\d+[\.,]?\d*)'
        padrao3 = r'(\d+[\.,]?\d*)\s*\/\s*km.*?(\d+[\.,]?\d*)'
        padrao4 = r'(\d+)[\.,:](\d+).*?(\d+)[\.,:](\d+)'
        
        match = (re.search(padrao1, objetivo) or 
                 re.search(padrao2, objetivo) or 
                 re.search(padrao3, objetivo) or
                 re.search(padrao4, objetivo))
        
        if not match:
            return "Formato de objetivo inválido. Exemplos válidos:<br>" \
                   "- Reduzir pace de 6:30 para 5:45<br>" \
                   "- 6.5 min/km para 5.8 min/km<br>" \
                   "- Melhorar ritmo de 7min/km para 6min30/km", 400
        
        if len(match.groups()) == 4:
            pace_inicial = float(f"{match.group(1)}.{match.group(2)}")
            pace_final = float(f"{match.group(3)}.{match.group(4)}")
        else:
            pace_inicial = float(match.group(1).replace(',', '.'))
            pace_final = float(match.group(2).replace(',', '.'))
            
    except Exception as e:
        logger.error(f"Erro ao processar objetivo: {e}")
        return "Erro ao processar seu objetivo. Por favor, verifique o formato e tente novamente.", 400

    prompt = f"""
    Crie um plano de MELHORIA DE RITMO que GARANTA a evolução de {pace_inicial}min/km para {pace_final}min/km 
    em {dados_usuario['tempo_melhoria']}. Siga exatamente:

    REQUISITOS:
    1. RESULTADO FINAL:
       - Na semana {semanas}, o usuário deve conseguir correr consistentemente a {pace_final}min/km
    
    2. TREINOS DIÁRIOS:
       - Detalhe cada sessão com:
         * Ritmos específicos para cada segmento
         * Exemplo: 
           "Quarta-feira:
           - 10' aquecimento (6:40/km)
           - 6x800m a 4:50/km (recuperação 400m a 7:00/km)
           - 10' desaquecimento (6:30/km)"
    
    3. PROGRESSÃO MENSURÁVEL:
       - Mostre a melhoria semanal do pace
       - Exemplo:
         "Semana 1: Intervalos a 5:30/km
         Semana 4: Intervalos a 5:10/km
         Semana 8: Intervalos a 4:50/km (ritmo-alvo)"
    
    4. PERSONALIZAÇÃO:
       - Nível: {dados_usuario['nivel']}
       - Dias/semana: {dados_usuario['dias']}
       - Tempo/sessão: {dados_usuario['tempo']} minutos

    FORMATO DE RESPOSTA:
    - Semana a semana com evolução clara
    - Ritmos precisos em todos os treinos
    - Garantia de que o pace-alvo será alcançado
    """

    plano_gerado = await gerar_plano_openai(prompt, semanas)
    registrar_geracao(email, plano)

    session["titulo"] = f"Plano de Pace: {pace_inicial}min/km → {pace_final}min/km"
    session["plano"] = DISCLAIMER + plano_gerado
    return redirect(url_for("resultado"))

# ================================================
# ROTAS DE PAGAMENTO E WEBHOOK (COMPLETO)
# ================================================

@app.route("/iniciar_pagamento", methods=["GET", "POST"])
def iniciar_pagamento():
    dados_usuario = request.form if request.method == "POST" else request.args

    if "email" not in dados_usuario:
        return "Email não fornecido.", 400

    email = dados_usuario["email"]

    try:
        db.execute(
            text("INSERT INTO tentativas_pagamento (email, data_tentativa) VALUES (:email, NOW())"),
            {"email": email}
        )
        db.commit()
    except Exception as e:
        logger.error(f"Erro ao registrar tentativa de pagamento: {e}")

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

    try:
        response = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers={
                "Authorization": f"Bearer {MERCADO_PAGO_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json=payload
        )
        response.raise_for_status()
        return redirect(response.json()["init_point"])
    except Exception as e:
        logger.error(f"Erro ao criar preferência de pagamento: {e}")
        return "Erro ao processar o pagamento. Tente novamente mais tarde.", 500

@app.route("/assinar_plano_anual", methods=["GET", "POST"])
def assinar_plano_anual():
    dados_usuario = request.form if request.method == "POST" else request.args

    if "email" not in dados_usuario:
        return "Email não fornecido.", 400

    email = dados_usuario["email"]

    try:
        db.execute(
            text("INSERT INTO usuarios (email, data_inscricao) VALUES (:email, NOW()) ON CONFLICT (email) DO NOTHING"),
            {"email": email}
        )
        db.commit()
    except Exception as e:
        logger.error(f"Erro ao registrar email: {e}")

    return redirect(url_for("iniciar_pagamento", email=email))

@app.route("/webhook/mercadopago", methods=["POST"])
def mercadopago_webhook():
    try:
        payload = request.json
        signature = request.headers.get("X-Signature")
        
        db.execute(
            text("INSERT INTO logs_webhook (payload, status_processamento) VALUES (:payload, 'recebido')"),
            {"payload": json.dumps(payload)}
        )
        db.commit()

        if not signature or not validar_assinatura(request.get_data(), signature):
            raise ValueError("Assinatura inválida")

        evento = payload.get("action")
        
        if evento == "payment.updated":
            payment_id = payload.get("data", {}).get("id")
            status = payload.get("data", {}).get("status")
            
            if not db.execute(text("SELECT 1 FROM pagamentos WHERE payment_id = :payment_id"), {"payment_id": payment_id}).fetchone():
                db.execute(
                    text("INSERT INTO pagamentos (payment_id, status, data_pagamento) VALUES (:payment_id, :status, NOW())"),
                    {"payment_id": payment_id, "status": status},
                )

        elif evento == "subscription.updated":
            subscription_id = payload.get("data", {}).get("id")
            status = payload.get("data", {}).get("status")
            email = payload.get("data", {}).get("payer", {}).get("email")
            nome = payload.get("data", {}).get("payer", {}).get("first_name", "Cliente")

            if email:
                usuario = db.execute(text("SELECT id FROM usuarios WHERE email = :email"), {"email": email}).fetchone()
                if not usuario:
                    db.execute(text("INSERT INTO usuarios (email, data_inscricao) VALUES (:email, NOW()) RETURNING id"), {"email": email})
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
        logger.error(f"Erro ao processar webhook: {str(e)}")
        db.execute(
            text("UPDATE logs_webhook SET status_processamento = 'erro', mensagem_erro = :mensagem WHERE id = (SELECT MAX(id) FROM logs_webhook)"),
            {"mensagem": str(e)}
        )
        db.commit()
        return jsonify({"error": str(e)}), 500

@app.route('/send_plan_email', methods=['POST'])
def send_plan_email():
    try:
        if not request.is_json:
            return jsonify({"success": False, "message": "Content-Type must be application/json"}), 400

        data = request.get_json()
        recipient = data.get('email')
        pdf_data = data.get('pdfData')
        
        tipo_treino = session.get("titulo", "Plano de Treino")
        nome_arquivo = f"TREINO_{tipo_treino.upper().replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.pdf"

        if not recipient or not pdf_data:
            return jsonify({"success": False, "message": "E-mail e PDF são obrigatórios"}), 400

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
        logger.info(f"E-mail enviado para: {recipient}")
        
        return jsonify({"success": True, "message": "E-mail enviado com sucesso!"})

    except Exception as e:
        logger.error(f"Erro ao enviar e-mail: {str(e)}")
        return jsonify({"success": False, "message": "Erro ao processar o envio do e-mail"}), 500

# ================================================
# INICIALIZAÇÃO
# ================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)