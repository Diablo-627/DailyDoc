FROM python:3.12-slim  # Явно указываем версию Python
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "DailyDoc.py"]
