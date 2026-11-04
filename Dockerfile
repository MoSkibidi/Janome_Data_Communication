FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV JANOME_HOST=0.0.0.0 \
    JANOME_PORT=8085 \
    JANOME_DATA_DIR=/data

EXPOSE 8085

CMD ["python", "app.py"]
