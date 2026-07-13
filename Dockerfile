FROM node:22-alpine AS frontend-build
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY frontend ./frontend
RUN npm run build

FROM python:3.12-alpine
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY backend ./backend
COPY src ./src
COPY db ./db
COPY scripts ./scripts
COPY --from=frontend-build /build/frontend/dist ./frontend/dist
RUN mkdir /app/data
ENV PORT=3000 DATA_DIR=/app/data
EXPOSE 3000
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "3000"]
