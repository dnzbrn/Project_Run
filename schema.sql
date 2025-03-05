CREATE TABLE IF NOT EXISTS usuarios (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    plano VARCHAR(50) NOT NULL,
    data_inscricao TIMESTAMP NOT NULL,
    ultima_geracao TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS geracoes (
    id SERIAL PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
    data_geracao TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS pagamentos (
    id SERIAL PRIMARY KEY,
    payment_id VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL,
    data_atualizacao TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS assinaturas (
    id SERIAL PRIMARY KEY,
    subscription_id VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL,
    data_atualizacao TIMESTAMP NOT NULL
);