FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY src/ src/
COPY abay-vault/ abay-vault/

CMD ["python", "-m", "abay_assistant.main"]
