FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium and its OS-level dependencies for Playwright.
# This is the heavy layer (~400MB) that makes this the "pro" build.
RUN playwright install --with-deps chromium

COPY server.py .

EXPOSE 8082

CMD ["python", "server.py", "--sse", "--host", "0.0.0.0", "--port", "8082"]
