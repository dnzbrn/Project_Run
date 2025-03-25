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

# ================================================
# CONFIGURAÇÕES INICIAIS
# ================================================

# Configuração do Flask
template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Templates")
app = Flask(__name__, template_folder=template_dir)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "uma_chave_segura_aqui")

# Configuração do Banco de Dados PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)
db = scoped_session(sessionmaker(bind=engine))

# Configuração do Mercado Pago
MERCADO_PAGO_ACCESS_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN")
MERCADO_PAGO_WEBHOOK_SECRET = os.getenv("MERCADO_PAGO_WEBHOOK_SECRET")

# Configuração da OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Configuração do Flask-Mail para Zoho Mail
app.config['MAIL_SERVER'] = 'smtp.zoho.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('ZOHO_EMAIL')
app.config['MAIL_PASSWORD'] = os.getenv('ZOHO_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('ZOHO_EMAIL')
mail = Mail(app)

# Variáveis globais
DISCLAIMER = "Este plano é gerado automaticamente. Consulte um profissional para ajustes personalizados.\n\n"

# Configuração de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================================================
# FUNÇÕES AUXILIARES
# ================================================

def validar_assinatura(body, signature):
    """Valida a assinatura do webhook do Mercado Pago"""
    if not MERCADO_PAGO_WEBHOOK_SECRET:
        raise ValueError("Assinatura secreta do webhook não configurada.")
    chave_secreta = MERCADO_PAGO_WEBHOOK_SECRET.encode("utf-8")
    assinatura_esperada = hmac.new(chave_secreta, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(assinatura_esperada, signature)

def pode_gerar_plano(email, plano):
    """Verifica se o usuário pode gerar um novo plano"""
    usuario = db.execute(
        text("SELECT * FROM usuarios WHERE email = :email"),
        {"email": email}
    ).mappings().fetchone()

    if plano == "anual":
        if usuario:
            assinatura = db.execute(
                text("SELECT status FROM assinaturas WHERE usuario_id = :usuario_id ORDER BY id DESC LIMIT 1"),
                {"usuario_id": usuario["id"]}
            ).mappings().fetchone()
            return assinatura and assinatura["status"] == "active"
        return False

    elif plano == "gratuito":
        if usuario and usuario["ultima_geracao"]:
            ultima_geracao = datetime.strptime(str(usuario["ultima_geracao"]), "%Y-%m-%d %H:%M:%S")
            return (datetime.now() - ultima_geracao).days >= 30
        return True

    return False

def calcular_semanas(tempo_melhoria):
    """Calcula o número de semanas com base no tempo de melhoria informado"""
    try:
        tempo_melhoria = tempo_melhoria.lower()
        
        # Padroniza a entrada para facilitar o parsing
        tempo_melhoria = tempo_melhoria.replace("meses", "mês").replace("mese", "mês")
        
        # Extrai valor e unidade
        match = re.search(r'(\d+)\s*(semanas?|meses?|mês|m)', tempo_melhoria)
        
        if not match:
            return 4  # Default
        
        valor = int(match.group(1))
        unidade = match.group(2)
        
        if 'semana' in unidade:
            return min(max(valor, 2), 52)  # Mínimo 2 semanas, máximo 1 ano
        elif 'mes' in unidade or 'mês' in unidade or 'm' in unidade:
            return min(max(valor * 4, 4), 52)  # 4 semanas por mês, mínimo 4 semanas
        else:
            return 4  # Default
    except Exception as e:
        logger.error(f"Erro ao calcular semanas: {e}")
        return 4  # Default em caso de erro

def registrar_geracao(email, plano):
    """Registra a geração de um novo plano no banco de dados"""
    hoje = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    usuario = db.execute(
        text("SELECT * FROM usuarios WHERE email = :email"),
        {"email": email}
    ).mappings().fetchone()

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
        usuario_id = usuario["id"]

    db.execute(
        text("INSERT INTO geracoes (usuario_id, data_geracao) VALUES (:usuario_id, :data_geracao)"),
        {"usuario_id": usuario_id, "data_geracao": hoje},
    )
    db.commit()

def gerar_plano_openai(prompt, semanas):
    """Gera o plano de treino usando a API da OpenAI com lógica de resumo"""
    try:
        # Configurações baseadas no tamanho do plano
        model = "gpt-3.5-turbo-16k" if semanas > 12 else "gpt-3.5-turbo"
        max_tokens = 4000 if semanas > 12 else 2000
        
        # Adiciona instruções mais claras ao prompt
        prompt_enhanced = prompt + f"""
        
        REGRAS ABSOLUTAS PARA {semanas} SEMANAS:
        1. Para {semanas} semanas ou menos, DETALHE CADA SEMANA INDIVIDUALMENTE
        2. Sempre inclua:
           - Objetivo específico da semana
           - Todos os dias de treino com:
             * Tipo (leve/moderado/intenso)
             * PACE ALVO EXATO (em min/km) ou TEMPO TOTAL (para planos de corrida)
             * Estrutura temporal detalhada
             * Descrição completa
             * Sensação esperada
           - Dias de descanso
        3. Progressão realista semana a semana
        4. Nunca resuma semanas individuais quando o total for ≤12 semanas
        """
        
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Você é um treinador de corrida experiente. Siga exatamente as instruções fornecidas."},
                {"role": "user", "content": prompt_enhanced},
            ],
            max_tokens=max_tokens,
            temperature=0.7,
        )
        plano = response.choices[0].message.content.strip()
        
        # Verificação adicional para garantir que todas as semanas foram geradas
        if semanas <= 12:
            semana_count = plano.lower().count("semana")
            if semana_count < semanas:
                # Se faltam semanas, solicita complemento
                complemento = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "Você está completando um plano de treino."},
                        {"role": "user", "content": f"Complete o plano com as semanas faltantes ({semana_count+1} a {semanas}):\n{plano}"},
                    ],
                    max_tokens=2000,
                    temperature=0.7,
                )
                plano += "\n\n" + complemento.choices[0].message.content.strip()
                
        return plano
    except Exception as e:
        logger.error(f"Erro ao gerar plano com OpenAI: {e}")
        return "Erro ao gerar o plano. Tente novamente mais tarde."

def enviar_email_confirmacao_pagamento(email, nome="Cliente"):
    """Envia e-mail de confirmação de pagamento"""
    try:
        email_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: 'Arial', sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background-color: #2563eb; padding: 20px; text-align: center; border-radius: 8px 8px 0 0; }}
                .content {{ padding: 20px; background-color: #f9fafb; border-radius: 0 0 8px 8px; }}
                .footer {{ text-align: center; font-size: 12px; color: #666; margin-top: 20px; }}
                .btn {{ display: inline-block; background-color: #2563eb; color: #ffffff !important; padding: 12px 24px; text-decoration: none; border-radius: 5px; margin: 15px 0; font-weight: bold; }}
                h1 {{ color: white; margin: 0; font-size: 24px; }}
                h2 {{ color: #2563eb; margin-top: 0; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>TreinoRun</h1>
            </div>
            <div class="content">
                <h2>Confirmação de Pagamento</h2>
                <p>Olá {nome},</p>
                <p>Seu pagamento para o Plano Anual foi confirmado com sucesso!</p>
                <div style="background-color: #e6f0ff; padding: 10px; border-radius: 5px; margin: 15px 0;">
                    <p><strong>Detalhes da Assinatura:</strong></p>
                    <p>• Plano: Anual</p>
                    <p>• Valor: R$ 59,90</p>
                    <p>• Próximo vencimento: {(datetime.now() + timedelta(days=365)).strftime('%d/%m/%Y')}</p>
                </div>
                <p>Agora você pode gerar planos de treino ilimitados durante 1 ano.</p>
                <a href="https://treinorun.com.br/seutreino" class="btn">Criar Meu Primeiro Treino</a>
                <p>Atenciosamente,<br>Equipe TreinoRun</p>
            </div>
            <div class="footer">
                <p>TreinoRun © {datetime.now().year} | Todos os direitos reservados</p>
            </div>
        </body>
        </html>
        """

        msg = Message(
            subject="Confirmação de Pagamento - Plano Anual",
            recipients=[email],
            html=email_html
        )
        mail.send(msg)
        logger.info(f"E-mail de confirmação enviado para: {email}")
        return True
    except Exception as e:
        logger.error(f"Erro ao enviar e-mail de confirmação: {str(e)}")
        return False

# ================================================
# ROTAS PRINCIPAIS
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
# ROTAS DE GERACAO DE PLANOS (MELHORADAS)
# ================================================

@app.route("/generate", methods=["POST"])
def generate():
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
    
    # Validação adicional para objetivos muito agressivos
    try:
        if "maratona" in dados_usuario['objetivo'].lower() and semanas < 16:
            return "Preparação para maratona requer mínimo de 16 semanas. Ajuste seu tempo de melhoria.", 400
        elif "meia-maratona" in dados_usuario['objetivo'].lower() and semanas < 12:
            return "Preparação para meia-maratona requer mínimo de 12 semanas. Ajuste seu tempo de melhoria.", 400
    except:
        pass

    prompt = f"""
    Crie um plano de corrida para {dados_usuario['objetivo']} em {dados_usuario['tempo_melhoria']},
    {dados_usuario['dias']} dias/semana, sessões de {dados_usuario['tempo']} minutos.

    NÍVEL: {dados_usuario['nivel']}
    DIAS DISPONÍVEIS: {dados_usuario['dias']}
    TEMPO POR SESSÃO: {dados_usuario['tempo']} minutos
    TOTAL DE SEMANAS: {semanas}

    INSTRUÇÕES ESPECÍFICAS:
    1. DETALHE TODAS AS {semanas} SEMANAS INDIVIDUALMENTE
    2. Cada semana deve conter:
       - Objetivo específico da semana
       - Todos os dias de treino com:
         * Tipo (leve/moderado/intenso)
         * TEMPO TOTAL: [min]
         * DIVISÃO TEMPORAL: (ex: 5min aquecimento + 20min principal + 5min desaquecimento)
         * Descrição completa da sessão
         * Sensação esperada (ex: "confortável", "desafiador")
       - Dias de descanso
    3. Progressão gradual e segura semana a semana
    4. Incluir variação de treinos (longos, intervalados, recuperação, ritmo)

    EXEMPLOS CONCRETOS REQUERIDOS:
    - Moderado: 10min caminhada + 15min corrida (5min a 6:30/km + 10min a 6:15/km) + 5min caminhada
    - Intenso: 5min aquecimento + 8x(2min a 5:45/km + 1min trote) + 5min desaquecimento
    - Leve: 30min contínuo a 7:00/km
    - Longo: 60min progressivo (20min a 6:30/km + 20min a 6:15/km + 20min a 6:00/km)

    REGRAS ABSOLUTAS:
    1. NUNCA use "similar às semanas anteriores"
    2. SEMPRE especifique tempos EXATOS para cada sessão
    3. Garanta progressão lógica e segura
    4. Inclua todos os {dados_usuario['dias']} dias de treino por semana
    5. Nunca resuma semanas individuais quando o total for ≤12
    """
    
    plano_gerado = gerar_plano_openai(prompt, semanas)
    registrar_geracao(email, plano)

    session["titulo"] = "Plano de Corrida"
    session["plano"] = DISCLAIMER + plano_gerado
    return redirect(url_for("resultado"))

@app.route("/generatePace", methods=["POST"])
def generatePace():
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
    
    # Validação adicional para progressão de pace realista
    try:
        pace_inicial = float(dados_usuario['objetivo'].split("para")[0].strip())
        pace_final = float(dados_usuario['objetivo'].split("para")[1].strip())
        
        # Limita a melhoria máxima de 0.5 min/km por semana
        melhoria_semanal = min((pace_inicial - pace_final)/semanas, 0.5)
        if melhoria_semanal > 0.5:
            return "A melhoria solicitada é muito agressiva. Por favor, ajuste seu objetivo para uma progressão mais segura.", 400
    except:
        pass

    prompt = f"""
    Crie um plano para melhorar o pace de {dados_usuario['objetivo']} em {dados_usuario['tempo_melhoria']},
    {dados_usuario['dias']} dias/semana, sessões de {dados_usuario['tempo']} minutos.

    NÍVEL: {dados_usuario['nivel']}
    DIAS DISPONÍVEIS: {dados_usuario['dias']}
    TEMPO POR SESSÃO: {dados_usuario['tempo']} minutos
    TOTAL DE SEMANAS: {semanas}

    INSTRUÇÕES ESPECÍFICAS:
    1. DETALHE TODAS AS {semanas} SEMANAS INDIVIDUALMENTE
    2. Cada semana deve conter:
       - Objetivo de pace específico
       - Todos os dias de treino com:
         * Tipo (leve/moderado/intenso)
         * PACE ALVO: [valor exato em min/km]
         * ESTRUTURA TEMPORAL: (ex: 5min a 6:30/km + 20min a 6:00/km)
         * Descrição completa da sessão
         * Sensação esperada (ex: "confortável", "desafiador")
       - Dias de descanso
    3. Progressão realista semana a semana (máximo 0.5 min/km de melhoria por semana)
    4. Incluir variação de treinos (longos, intervalados, recuperação)

    EXEMPLOS CONCRETOS REQUERIDOS:
    - Intervalado: 5min a 6:30/km + 6x(400m a 5:00/km com 200m trote) + 5min a 6:30/km
    - Longo: 10km progressivo (5km a 6:15/km + 5km a 6:00/km)
    - Recuperação: 5km a 7:00/km

    REGRAS ABSOLUTAS:
    1. NUNCA resuma semanas quando o total for ≤12
    2. SEMPRE especifique paces EXATOS para cada sessão
    3. Garanta progressão lógica e segura
    4. Inclua todos os {dados_usuario['dias']} dias de treino por semana
    """
    
    plano_gerado = gerar_plano_openai(prompt, semanas)
    registrar_geracao(email, plano)

    session["titulo"] = "Plano de Pace"
    session["plano"] = DISCLAIMER + plano_gerado
    return redirect(url_for("resultado"))

# ================================================
# ROTAS DE PAGAMENTO E ASSINATURA
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

# ================================================
# WEBHOOK E EMAIL
# ================================================

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
            return jsonify({
                "success": False,
                "message": "Content-Type must be application/json"
            }), 400

        data = request.get_json()
        recipient = data.get('email')
        pdf_data = data.get('pdfData')
        
        tipo_treino = session.get("titulo", "Plano de Treino")
        tipo_treino_simplificado = tipo_treino.replace("Plano de ", "")
        data_atual = datetime.now().strftime("%Y%m%d")
        nome_arquivo = f"TREINO_{tipo_treino_simplificado.upper().replace(' ', '_')}_{data_atual}.pdf"

        if not recipient or not pdf_data:
            return jsonify({
                "success": False,
                "message": "E-mail e PDF são obrigatórios"
            }), 400

        email_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: 'Arial', sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background-color: #2563eb; padding: 20px; text-align: center; border-radius: 8px 8px 0 0; }}
                .content {{ padding: 20px; background-color: #f9fafb; border-radius: 0 0 8px 8px; }}
                .footer {{ text-align: center; font-size: 12px; color: #666; margin-top: 20px; }}
                .btn {{ display: inline-block; background-color: #2563eb; color: #ffffff !important; padding: 12px 24px; text-decoration: none; border-radius: 5px; margin: 15px 0; font-weight: bold; }}
                h1 {{ color: white; margin: 0; font-size: 24px; }}
                h2 {{ color: #2563eb; margin-top: 0; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>TreinoRun</h1>
            </div>
            <div class="content">
                <h2>Seu {tipo_treino} Personalizado</h2>
                <p>Segue em anexo o seu {tipo_treino.lower()} personalizado.</p>
                <p>Você pode acessar nosso site para mais informações:</p>
                <a href="https://treinorun.com.br" class="btn">Acessar TreinoRun</a>
                <p>Atenciosamente,<br>Equipe TreinoRun</p>
            </div>
            <div class="footer">
                <p>TreinoRun © {datetime.now().year} | Todos os direitos reservados</p>
            </div>
        </body>
        </html>
        """

        msg = Message(
            subject=f"Seu {tipo_treino} - TreinoRun",
            recipients=[recipient],
            html=email_html
        )

        pdf_content = base64.b64decode(pdf_data.split(',')[1])
        msg.attach(
            nome_arquivo,
            "application/pdf",
            BytesIO(pdf_content).read()
        )

        mail.send(msg)
        logger.info(f"E-mail enviado para: {recipient}")
        
        return jsonify({
            "success": True,
            "message": "E-mail enviado com sucesso!"
        })

    except Exception as e:
        logger.error(f"Erro ao enviar e-mail: {str(e)}")
        return jsonify({
            "success": False,
            "message": "Erro ao processar o envio do e-mail"
        }), 500

# ================================================
# INICIALIZAÇÃO
# ================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=True, host="0.0.0.0", port=port)