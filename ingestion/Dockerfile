FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p incoming processed staging

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["python", "app.py"]
