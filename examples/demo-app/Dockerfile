FROM python:3.12-alpine

WORKDIR /app
COPY app.py ./app.py
HEALTHCHECK --interval=2s --timeout=2s --start-period=2s --retries=10 \
  CMD wget --quiet --spider http://127.0.0.1:8080/health || exit 1
EXPOSE 8080
CMD ["python", "app.py"]

