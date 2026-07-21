FROM python:3.11-slim

WORKDIR /app

# Install dependencies needed for package installation
RUN pip install --no-cache-dir --upgrade pip

# Copy the package source
COPY . /app

# Install the package in editable or standard mode
RUN pip install .

# Expose the API/WS port and the dynamic SSH forwarding port range
EXPOSE 8000 8001 2200-2300

# Environment variables
ENV RAYTUNNEL_TOKEN="change-me"
ENV HOST="0.0.0.0"
ENV PORT="8001"
ENV TCP_PORT="8000"
ENV DOMAIN="s.yourdomain.com"

# Command to run the server
CMD raytunnel server --host $HOST --port $PORT --tcp-port $TCP_PORT --token $RAYTUNNEL_TOKEN --domain $DOMAIN
