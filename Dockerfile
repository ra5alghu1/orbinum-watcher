FROM python:3.12-alpine

WORKDIR /app

COPY web.py collector_health.py /app/

EXPOSE 8787

CMD ["python3", "/app/web.py"]
