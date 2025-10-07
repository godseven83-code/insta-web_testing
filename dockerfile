# Use an official lightweight Python image
FROM python:3.11-slim

# Install system dependencies (ffmpeg, etc.)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy project files into container
COPY . /app

# Install Python dependencies
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Expose the app port
ENV PORT=5000
EXPOSE 5000

# Start the Flask app with Gunicorn
CMD ["gunicorn", "main_web:app", "--bind", "0.0.0.0:5000"]
