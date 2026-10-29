FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backends.py backends.yaml scribe.py ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "scribe.py"]
