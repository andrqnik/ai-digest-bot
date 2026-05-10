FROM python:3.12-slim

WORKDIR /app

# Install DejaVu fonts for Cyrillic/Unicode support in PDF generation
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Run the bot
CMD ["python", "bot.py"]
