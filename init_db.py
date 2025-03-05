from run import app, get_db

def init_db():
    with app.app_context():
        db = get_db()
        with app.open_resource("schema.sql", mode="r") as f:
            db.execute(f.read())
        db.commit()
        print("Banco de dados inicializado com sucesso!")

if __name__ == "__main__":
    init_db()