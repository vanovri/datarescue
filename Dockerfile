FROM node:24-slim AS web
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml constraints.txt README.md LICENSE ./
COPY datarescue/ ./datarescue/
COPY examples/ ./examples/
COPY --from=web /build/frontend/dist ./frontend/dist/
RUN pip install --no-cache-dir -c constraints.txt . && useradd --create-home appuser
USER appuser
EXPOSE 8763
CMD ["python", "-m", "uvicorn", "datarescue.api:app", "--host", "0.0.0.0", "--port", "8763"]
