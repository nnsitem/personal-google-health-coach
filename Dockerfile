FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY coach/ coach/

ENV COACH_DATA_DIR=/app/data

EXPOSE 8080
CMD ["uvicorn", "coach.main:app", "--host", "0.0.0.0", "--port", "8080"]
