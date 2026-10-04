FROM python:3.12-slim
WORKDIR /app
COPY server.py /app/server.py
COPY static /app/static
ENV PORT=8080 PYTHONUNBUFFERED=1 MALLOC_ARENA_MAX=2 MALLOC_MMAP_THRESHOLD_=131072
EXPOSE 8080
CMD ["python", "/app/server.py"]
