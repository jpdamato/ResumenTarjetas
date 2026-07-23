FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Las dependencias primero, para que Docker cachee esta capa y no reinstale
# pdfplumber cada vez que cambia el codigo.
COPY analizador/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY analizador/ /app/
COPY docker/entrypoint.sh /entrypoint.sh

# Si el repo se clono en Windows, el script puede tener finales de linea CRLF
# y /bin/sh falla con un "not found" muy poco descriptivo.
RUN sed -i 's/\r$//' /entrypoint.sh \
 && chmod +x /entrypoint.sh \
 && mkdir -p /web /salida /datos \
 && useradd --create-home --uid 1000 app \
 && chown -R app:app /web /salida /datos /app

USER app
EXPOSE 8080

ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
