FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY trade ./trade
RUN useradd --create-home trader && mkdir /app/data && chown trader:trader /app/data
USER trader
EXPOSE 8000
CMD ["uvicorn", "trade.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
