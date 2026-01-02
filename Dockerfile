FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# render.com автоматически устанавливает PORT
EXPOSE 8000

CMD ["python", "bot.py"]
