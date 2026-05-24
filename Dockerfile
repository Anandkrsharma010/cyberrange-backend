FROM python:3.10-slim

# Install system dependencies, including curl, git, unzip (for Terraform)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Terraform CLI
RUN curl -fsSL https://releases.hashicorp.com/terraform/1.9.5/terraform_1.9.5_linux_amd64.zip -o /tmp/terraform.zip \
    && unzip /tmp/terraform.zip -d /usr/local/bin/ \
    && rm /tmp/terraform.zip

WORKDIR /app

# Copy dependency list
COPY requirements.txt .

# Install python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source code
COPY . .

EXPOSE 8000

# Default command runs database initialization and then uvicorn
CMD ["./start.sh"]

