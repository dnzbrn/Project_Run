from flask import Flask, request, render_template, redirect, url_for, session, jsonify
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker
from datetime import datetime
import hmac
import hashlib
import openai

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
openai.api_key = os.getenv("OPENAI_API_KEY")

# Variável de disclaimer
DISCLAIMER = "Este plano é gerado automaticamente. Consulte um profissional para ajustes personalizados.\n\n"

# Função para conectar ao banco de dados
def get_db():
    return db

# Função para validar a assinatura do webhook
def validar_assinatura(body, signature):
    """
    Valida a assinatura do webhook usando a chave secreta.
    """
    if not MERCADO_PAGO_WEBHOOK_SECRET:
        raise ValueError("Assinatura secreta do webhook não configurada.")

    # Gera a assinatura esperada
    chave_secreta = MERCADO_PAGO_WEBHOOK_SECRET.encode("utf-8")
    assinatura_esperada = hmac.new(chave_secreta, body, hashlib.sha256).hexdigest()

    # Compara a assinatura recebida com a esperada
    return hmac.compare_digest(assinatura_esperada, signature)

# Função para verificar se o usuário pode gerar um plano
def pode_gerar_plano(email, plano):
    db = get_db()
    usuario = db.execute(
        text("SELECT * FROM usuarios WHERE email = :email"),
        {"email": email}
    ).fetchone()

    if not usuario:
        # Usuário não existe, pode gerar plano
        return True

    if plano == "anual":
        # Plano anual: pode gerar quantas vezes quiser
        return True
    elif plano == "gratuito":
        # Plano gratuito: verifica se já gerou um plano este mês
        ultima_geracao = datetime.strptime(usuario["ultima_geracao"], "%Y-%m-%d %H:%M:%S")
        hoje = datetime.now()
        if (hoje - ultima_geracao).days < 30:
            return False  # Já gerou um plano este mês
        return True

    return False

# Função para registrar a geração de um plano
def registrar_geracao(email, plano):
    db = get_db()
    hoje = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Verifica se o usuário já existe
    usuario = db.execute(
        text("SELECT * FROM usuarios WHERE email = :email"),
        {"email": email}
    ).fetchone()

    if not usuario:
        # Cria um novo usuário
        db.execute(
            text("""
                INSERT INTO usuarios (email, plano, data_inscricao, ultima_geracao)
                VALUES (:email, :plano, :data_inscricao, :ultima_geracao)
            """),
            {"email": email, "plano": plano, "data_inscricao": hoje, "ultima_geracao": hoje},
        )
        db.commit()

        # Busca o ID do usuário recém-inserido
        usuario = db.execute(
            text("SELECT id FROM usuarios WHERE email = :email"),
            {"email": email}
        ).fetchone()
        usuario_id = usuario["id"]
    else:
        # Atualiza a última geração
        db.execute(
            text("UPDATE usuarios SET ultima_geracao = :ultima_geracao WHERE email = :email"),
            {"ultima_geracao": hoje, "email": email},
        )
        usuario_id = usuario["id"]  # Usa o ID do usuário existente

    # Registra a geração na tabela de gerações
    db.execute(
        text("INSERT INTO geracoes (usuario_id, data_geracao) VALUES (:usuario_id, :data_geracao)"),
        {"usuario_id": usuario_id, "data_geracao": hoje},
    )
    db.commit()

# Função para gerar o plano de treino usando a API da OpenAI
def gerar_plano_openai(prompt):
    """
    Gera um plano de treino usando a API da OpenAI.
    """
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",  # Ou "gpt-3.5-turbo" para um modelo mais rápido
            messages=[
                {"role": "system", "content": "Você é um treinador de corrida experiente."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1000,  # Ajuste conforme necessário
            temperature=0.7,  # Controla a criatividade da resposta
        )
        return response.choices[0].message["content"].strip()
    except Exception as e:
        print(f"Erro ao gerar plano com OpenAI: {e}")
        return "Erro ao gerar o plano. Tente novamente mais tarde."

# Rota da página principal (Landing Page)
@app.route("/")
def landing():
    return render_template("landing.html")

# Rota de um segundo formulário (seutreino)
@app.route("/seutreino")
def seutreino():
    return render_template("seutreino.html")

# Processa o formulário de CORRIDA
@app.route("/generate", methods=["POST"])
def generate():
    dados_usuario = request.form

    # Verifica se todos os campos necessários foram enviados
    required_fields = ["email", "plano", "objetivo", "tempo_melhoria", "nivel", "dias", "tempo"]
    if not all(field in dados_usuario for field in required_fields):
        return "Dados do formulário incompletos.", 400

    email = dados_usuario["email"]
    plano = dados_usuario["plano"]

    # Verifica se o usuário pode gerar um plano
    if not pode_gerar_plano(email, plano):
        return "Você já gerou um plano gratuito este mês. Atualize para o plano anual para gerar mais planos.", 400

    # Gera o plano de treino
    prompt = f"""
Crie um plano detalhado de corrida para atingir o objetivo de {dados_usuario['objetivo']} em {dados_usuario['tempo_melhoria']}:
- Nível: {dados_usuario['nivel']}
- Dias disponíveis: {dados_usuario['dias']}
- Tempo diário: {dados_usuario['tempo']} minutos.

Para cada treino, forneça:
- Tipo de exercício (ex.: caminhada leve, corrida moderada, intervalados, etc.);
- Pace (ritmo de corrida) sugerido;
- Tempo de duração do treino.

Estruture o plano de forma semanal, listando os treinos por dia.
    """
    plano_gerado = gerar_plano_openai(prompt)

    # Registra a geração do plano
    registrar_geracao(email, plano)

    session["titulo"] = "Plano de Corrida"
    session["plano"] = DISCLAIMER + plano_gerado
    return redirect(url_for("resultado"))

# Processa o formulário de PACE
@app.route("/generatePace", methods=["POST"])
def generatePace():
    dados_usuario = request.form

    # Verifica se todos os campos necessários foram enviados
    required_fields = ["email", "plano", "objetivo", "tempo_melhoria", "nivel", "dias", "tempo"]
    if not all(field in dados_usuario for field in required_fields):
        return "Dados do formulário incompletos.", 400

    email = dados_usuario["email"]
    plano = dados_usuario["plano"]

    # Verifica se o usuário pode gerar um plano
    if not pode_gerar_plano(email, plano):
        return "Você já gerou um plano gratuito este mês. Atualize para o plano anual para gerar mais planos.", 400

    # Gera o plano de pace
    prompt = f"""
Crie um plano detalhado para melhorar o pace de {dados_usuario['objetivo']} em {dados_usuario['tempo_melhoria']}:
- Nível: {dados_usuario['nivel']}
- Dias disponíveis: {dados_usuario['dias']}
- Tempo diário: {dados_usuario['tempo']} minutos.

Para cada treino, forneça:
- Tipo de exercício (ex.: corrida leve, intervalados, tiros, etc.);
- Pace (ritmo de corrida) sugerido;
- Tempo de duração do treino.

Estruture o plano de forma semanal, listando os treinos por dia.
    """
    plano_gerado = gerar_plano_openai(prompt)

    # Registra a geração do plano
    registrar_geracao(email, plano)

    session["titulo"] = "Plano de Pace"
    session["plano"] = DISCLAIMER + plano_gerado
    return redirect(url_for("resultado"))

# Página de resultados
@app.route("/resultado")
def resultado():
    titulo = session.get("titulo", "Plano de Treino")
    plano = session.get("plano", "Nenhum plano gerado.")
    return render_template("resultado.html", titulo=titulo, plano=plano)

# Rota do Webhook do Mercado Pago
@app.route("/webhook/mercadopago", methods=["POST"])
def mercadopago_webhook():
    try:
        # Verifica se a assinatura foi enviada
        signature = request.headers.get("X-Signature")
        if not signature:
            return jsonify({"error": "Assinatura inválida"}), 403

        # Valida a assinatura
        body = request.get_data()  # Obtém o corpo da requisição
        if not validar_assinatura(body, signature):
            return jsonify({"error": "Assinatura inválida"}), 403

        # Processa os dados do webhook
        data = request.json
        print("Dados recebidos do Mercado Pago:", data)

        # Verifica o tipo de evento
        evento = data.get("action")
        if evento == "payment.updated":
            payment_id = data.get("data", {}).get("id")
            status = data.get("data", {}).get("status")
            print(f"Pagamento {payment_id} atualizado para o status: {status}")

            # Atualiza o status do pagamento no banco de dados
            db = get_db()
            db.execute(
                text("INSERT INTO pagamentos (payment_id, status) VALUES (:payment_id, :status)"),
                {"payment_id": payment_id, "status": status},
            )
            db.commit()

        elif evento == "subscription.updated":
            subscription_id = data.get("data", {}).get("id")
            status = data.get("data", {}).get("status")
            print(f"Assinatura {subscription_id} atualizada para o status: {status}")

            # Atualiza o status da assinatura no banco de dados
            db = get_db()
            db.execute(
                text("INSERT INTO assinaturas (subscription_id, status) VALUES (:subscription_id, :status)"),
                {"subscription_id": subscription_id, "status": status},
            )
            db.commit()

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print("Erro ao processar webhook:", str(e))
        return jsonify({"error": str(e)}), 500

# 🚀 Inicia o servidor Flask na porta correta para o Railway
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))  # Usa a porta dinâmica do Railway
    app.run(debug=True, host="0.0.0.0", port=port)