FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Commande pour lancer le serveur sur le port fourni par Render
CMD ["uvicorn", "moviebox_api_server:app", "--host", "0.0.0.0", "--port", "10000"]
