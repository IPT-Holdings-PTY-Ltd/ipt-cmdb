FROM python:3.12-alpine
WORKDIR /app
COPY app.py ./
COPY requirements.txt ./
COPY public ./public
COPY src ./src
COPY db ./db
COPY scripts ./scripts
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir /app/data
ENV PORT=3000 DATA_DIR=/app/data
EXPOSE 3000
CMD ["python", "app.py"]
