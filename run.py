from flask import Flask, request, render_template, redirect, url_for, session, jsonify
import os
import openai
import base64
import smtplib
from email.message import EmailMessage
from datetime import datetime

# Configuração do Flask
template_dir = os.path.abspath(r"C:\Users\brndi\Documents\Templates")
app = Flask(__name__, template_folder=template_dir)
app.secret_key = "super_secret_key"  # Necessário para usar session

# Configuração da API do OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")  # Use variáveis de ambiente para segredos

# Disclaimer a ser incluído no início do plano
DISCLAIMER = (
    "Disclaimer: Este plano é apenas uma sugestão e não substitui a avaliação e o acompanhamento "
    "de um profissional de saúde. Consulte um médico ou um profissional de educação física antes de "
    "iniciar qualquer programa de treinamento.\n\n"
)

# Função para gerar um plano no OpenAI
def gerar_plano(prompt):
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Você é um assistente especializado em criar planos de treino para corrida."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.5  # Temperatura reduzida para respostas mais objetivas
        )
        return response["choices"][0]["message"]["content"]
    except openai.error.OpenAIError as e:
        return f"Erro ao acessar a API OpenAI: {str(e)}"

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
    required_fields = ["objetivo", "tempo_melhoria", "nivel", "dias", "tempo"]
    if not all(field in dados_usuario for field in required_fields):
        return "Dados do formulário incompletos.", 400

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
    plano_gerado = gerar_plano(prompt)
    session["titulo"] = "Plano de Corrida"
    session["plano"] = DISCLAIMER + plano_gerado
    return redirect(url_for("resultado"))

# Processa o formulário de PACE
@app.route("/generatePace", methods=["POST"])
def generate_pace():
    dados_usuario = request.form

    # Verifica se todos os campos necessários foram enviados
    required_fields = ["objetivo", "tempo_melhoria", "nivel", "dias", "tempo"]
    if not all(field in dados_usuario for field in required_fields):
        return "Dados do formulário incompletos.", 400

    prompt = f"""
Crie um plano detalhado para melhorar o pace de {dados_usuario['objetivo']} em {dados_usuario['tempo_melhoria']}:
- Nível: {dados_usuario['nivel']}
- Dias disponíveis: {dados_usuario['dias']}
- Tempo por treino: {dados_usuario['tempo']} minutos.

Para cada treino, detalhe:
- Tipo de exercício (ex.: intervalados, progressivos, etc.);
- Pace ideal (ritmo de corrida) a ser mantido;
- Tempo recomendado de execução.

Organize o plano em uma estrutura semanal clara, indicando os treinos para cada dia.
    """
    plano_gerado = gerar_plano(prompt)
    session["titulo"] = "Plano para Melhorar o Pace"
    session["plano"] = DISCLAIMER + plano_gerado
    return redirect(url_for("resultado"))

# Página de resultados
@app.route("/resultado")
def resultado():
    titulo = session.get("titulo", "Plano de Treino")
    plano = session.get("plano", "Nenhum plano gerado.")
    return render_template("resultado.html", titulo=titulo, plano=plano)

# Endpoint para envio de e-mail com o PDF gerado
@app.route('/send_email', methods=['POST'])
def send_email():
    data = request.get_json()
    recipient = data.get('recipient')
    pdf_data = data.get('pdfData')

    if not recipient or not pdf_data:
        return jsonify({"status": "error", "message": "Dados incompletos"}), 400

    # Remove o prefixo do data URI, se presente
    if pdf_data.startswith("data:"):
        pdf_data = pdf_data.split(",", 1)[1]

    try:
        pdf_bytes = base64.b64decode(pdf_data)
    except Exception as e:
        return jsonify({"status": "error", "message": "Erro ao decodificar PDF: " + str(e)}), 400

    # Gerar nome do arquivo PDF baseado na data e tipo de treino
    today = datetime.now().strftime('%Y%m%d')
    titulo = session.get("titulo", "").lower()
    if "corrida" in titulo:
        training_type = "TREINO_CORRIDA"
    elif "pace" in titulo:
        training_type = "TREINO_PACE"
    else:
        training_type = "TREINO"
    filename = f"{today}_{training_type}.pdf"

    # Configurar o texto do e-mail
    email_text = (
        "Olá,\n\n"
        "Parabéns por tomar a iniciativa de cuidar da sua saúde! Em anexo, você encontrará o seu plano de treino personalizado.\n"
        "Siga as orientações com atenção e, se possível, consulte um profissional de saúde para acompanhamento.\n\n"
        "Desejamos muito sucesso em sua jornada!\n\n"
        "Atenciosamente,\n"
        "Equipe DNZ Dark Digital"
    )

    msg = EmailMessage()
    msg['Subject'] = 'Seu Plano de Treino'
    msg['From'] = 'dnzdarkdigital@gmail.com'
    msg['To'] = recipient
    msg.set_content(email_text)
    msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=filename)

    # Configuração do SMTP utilizando o Gmail (serviço gratuito)
    smtp_host = 'smtp.gmail.com'
    smtp_port = 587
    smtp_user = 'dnzdarkdigital@gmail.com'
    smtp_pass = os.getenv("SMTP_PASSWORD")  # Use variáveis de ambiente para segredos

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return jsonify({"status": "success", "message": "E-mail enviado com sucesso."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)