# FitPack MCP Server Dockerfile
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the MCP server code
COPY src/ ./src/
COPY mcp-config.json .
COPY openwebui-config.yaml .

# Copy fitpack modules (mount as volume in production)
# COPY /path/to/fitpack /fitpack

# Set environment variables
ENV PYTHONPATH=/app:/fitpack
ENV PYTHONUNBUFFERED=1

# Expose port for OpenWebUI integration
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.path.insert(0, '/fitpack'); from agents.parameter_tools import list_params; print('MCP server healthy')" || exit 1

# Default command
CMD ["python", "src/server.py", "--transport", "sse", "--port", "8000"]