FROM python:3.12-slim
RUN pip install --no-cache-dir "radicale[bcrypt]==3.3.3"
EXPOSE 5232
CMD ["radicale", "--config", "/config/config"]
