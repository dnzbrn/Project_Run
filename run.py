from flask import Flask, request, render_template, redirect, url_for, session, jsonify
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker
from datetime import datetime
import hmac
import hashlib
from openai import OpenAI
import requests
import json

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

# Variável de disclaimer
DISCLAIMER = "Este plano é gerado automaticamente. Consulte um profissional para ajustes personalizados.\n\n"

# Função para validar a assinatura do webhook
def validar_assinatura(body, signature):
    if not MERCADO_PAGO_WEBHOOK_SECRET:
        raise ValueError("Assinatura secreta do webhook não configurada.")

    chave_secreta = MERCADO_PAGO_WEBHOOK_SECRET.encode("utf-8")
    assinatura_esperada = hmac.new(chave_secreta, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(assinatura_esperada, signature)

# Função para verificar se o usuário pode gerar um plano
def pode_gerar_plano(email, plano):
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

            if assinatura and assinatura["status"] == "active":
                return True
        return False

    elif plano == "gratuito":
        if usuario:
            ultima_geracao = usuario["ultima_geracao"]
            if ultima_geracao:
                ultima_geracao = datetime.strptime(str(ultima_geracao), "%Y-%m-%d %H:%M:%S")
                hoje = datetime.now()
                if (hoje - ultima_geracao).days < 30:
                    return False
        return True

    return False

# Função para registrar a geração de um plano
def registrar_geracao(email, plano):
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
        db.commit()

        usuario = db.execute(
            text("SELECT id FROM usuarios WHERE email = :email"),
            {"email": email}
        ).mappings().fetchone()
        usuario_id = usuario["id"]
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

# Função para gerar o plano de treino
def gerar_plano_openai(prompt):
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Você é um treinador de corrida experiente."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1000,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Erro ao gerar plano com OpenAI: {e}")
        return "Erro ao gerar o plano. Tente novamente mais tarde."

# Rota para iniciar o pagamento no Mercado Pago
@app.route("/iniciar_pagamento", methods=["GET", "POST"])
def iniciar_pagamento():
    if request.method == "POST":
        dados_usuario = request.form
    else:
        dados_usuario = request.args

    if "email" not in dados_usuario:
        return "Email não fornecido.", 400

    email = dados_usuario["email"]

    # Registrar tentativa de pagamento
    try:
        db.execute(
            text("""
                INSERT INTO tentativas_pagamento (email, data_tentativa)
                VALUES (:email, NOW())
            """),
            {"email": email}
        )
        db.commit()
    except Exception as e:
        print(f"Erro ao registrar tentativa de pagamento: {e}")

    MERCADO_PAGO_URL = "https://api.mercadopago.com/checkout/preferences"
    headers = {
        "Authorization": f"Bearer {MERCADO_PAGO_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "items": [
            {
                "title": "Plano Anual de Treino",
                "quantity": 1,
                "unit_price": 59.9,
                "currency_id": "BRL",
            }
        ],
        "payer": {
            "email": email,
        },
        "back_urls": {
            "success": "https://treinorun.com.br/sucesso",
            "failure": "https://treinorun.com.br/erro",
            "pending": "https://treinorun.com.br/pendente",
        },
        "auto_return": "approved",
        "notification_url": "https://treinorun.com.br/webhook/mercadopago",
    }

    try:
        response = requests.post(MERCADO_PAGO_URL, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        return redirect(data["init_point"])
    except Exception as e:
        print(f"Erro ao criar preferência de pagamento: {e}")
        return "Erro ao processar o pagamento. Tente novamente mais tarde.", 500

# Rota para assinar o plano anual
@app.route("/assinar_plano_anual", methods=["GET", "POST"])
def assinar_plano_anual():
    if request.method == "POST":
        dados_usuario = request.form
    else:
        dados_usuario = request.args

    if "email" not in dados_usuario:
        return "Email não fornecido.", 400

    email = dados_usuario["email"]

    # Registrar o email no banco imediatamente
    try:
        db.execute(
            text("""
                INSERT INTO usuarios (email, data_inscricao)
                VALUES (:email, NOW())
                ON CONFLICT (email) DO NOTHING
            """),
            {"email": email}
        )
        db.commit()
    except Exception as e:
        print(f"Erro ao registrar email: {e}")

    return redirect(url_for("iniciar_pagamento", email=email))

# Rotas de páginas
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

# Processar formulário de CORRIDA
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
        else:
            return "Você já gerou um plano gratuito este mês. Atualize para o plano anual para gerar mais planos.", 400

    prompt = f"""
Crie um plano detalhado de corrida para atingir o objetivo de {dados_usuario['objetivo']} em {dados_usuario['tempo_melhoria']}:
- Nível: {dados_usuario['nivel']}
- Dias disponíveis: {dados_usuario['dias']}
- Tempo diário: {dados_usuario['tempo']} minutos.
    """
    plano_gerado = gerar_plano_openai(prompt)
    registrar_geracao(email, plano)

    session["titulo"] = "Plano de Corrida"
    session["plano"] = DISCLAIMER + plano_gerado
    return redirect(url_for("resultado"))

# Processar formulário de PACE
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
        else:
            return "Você já gerou um plano gratuito este mês. Atualize para o plano anual para gerar mais planos.", 400

    prompt = f"""
Crie um plano detalhado para melhorar o pace de {dados_usuario['objetivo']} em {dados_usuario['tempo_melhoria']}:
- Nível: {dados_usuario['nivel']}
- Dias disponíveis: {dados_usuario['dias']}
- Tempo diário: {dados_usuario['tempo']} minutos.
    """
    plano_gerado = gerar_plano_openai(prompt)
    registrar_geracao(email, plano)

    session["titulo"] = "Plano de Pace"
    session["plano"] = DISCLAIMER + plano_gerado
    return redirect(url_for("resultado"))

# Webhook do Mercado Pago
@app.route("/webhook/mercadopago", methods=["POST"])
def mercadopago_webhook():
    try:
        # Registrar recebimento do webhook
        payload = request.json
        signature = request.headers.get("X-Signature")
        
        db.execute(
            text("""
                INSERT INTO logs_webhook (payload, status_processamento)
                VALUES (:payload, 'recebido')
            """),
            {"payload": json.dumps(payload)}
        )
        db.commit()

        if not signature:
            raise ValueError("Assinatura não fornecida")

        if not validar_assinatura(request.get_data(), signature):
            raise ValueError("Assinatura inválida")

        # Atualizar status do log
        db.execute(
            text("""
                UPDATE logs_webhook
                SET status_processamento = 'processando'
                WHERE id = (SELECT MAX(id) FROM logs_webhook)
            """)
        )
        db.commit()

        evento = payload.get("action")
        
        if evento == "payment.updated":
            payment_id = payload.get("data", {}).get("id")
            status = payload.get("data", {}).get("status")
            
            # Verificar se já existe antes de inserir
            pagamento_existente = db.execute(
                text("SELECT 1 FROM pagamentos WHERE payment_id = :payment_id"),
                {"payment_id": payment_id}
            ).fetchone()
            
            if not pagamento_existente:
                db.execute(
                    text("""
                        INSERT INTO pagamentos (payment_id, status, data_pagamento)
                        VALUES (:payment_id, :status, NOW())
                    """),
                    {"payment_id": payment_id, "status": status},
                )
                db.commit()

        elif evento == "subscription.updated":
            subscription_id = payload.get("data", {}).get("id")
            status = payload.get("data", {}).get("status")
            email = payload.get("data", {}).get("payer", {}).get("email")

            if email:
                # Verificar se o usuário existe
                usuario = db.execute(
                    text("SELECT id FROM usuarios WHERE email = :email"),
                    {"email": email}
                ).fetchone()

                if not usuario:
                    # Criar usuário se não existir
                    db.execute(
                        text("""
                            INSERT INTO usuarios (email, data_inscricao)
                            VALUES (:email, NOW())
                            RETURNING id
                        """),
                        {"email": email}
                    )
                    usuario_id = db.fetchone()[0]
                    db.commit()
                else:
                    usuario_id = usuario[0]

                # Registrar/atualizar assinatura
                db.execute(
                    text("""
                        INSERT INTO assinaturas (subscription_id, usuario_id, status, data_atualizacao)
                        VALUES (:subscription_id, :usuario_id, :status, NOW())
                        ON CONFLICT (subscription_id) DO UPDATE
                        SET status = EXCLUDED.status,
                            data_atualizacao = NOW()
                    """),
                    {
                        "subscription_id": subscription_id,
                        "usuario_id": usuario_id,
                        "status": status
                    },
                )
                db.commit()

        # Registrar sucesso
        db.execute(
            text("""
                UPDATE logs_webhook
                SET status_processamento = 'sucesso'
                WHERE id = (SELECT MAX(id) FROM logs_webhook)
            """)
        )
        db.commit()

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print("Erro ao processar webhook:", str(e))
        db.execute(
            text("""
                UPDATE logs_webhook
                SET status_processamento = 'erro',
                    mensagem_erro = :mensagem
                WHERE id = (SELECT MAX(id) FROM logs_webhook)
            """),
            {"mensagem": str(e)}
        )
        db.commit()
        return jsonify({"error": str(e)}), 500

# Iniciar servidor
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=True, host="0.0.0.0", port=port)