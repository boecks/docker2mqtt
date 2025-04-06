FROM python:3.12-slim

# Copy requirements file and install dependencies
COPY requirements.txt /
RUN pip install --no-cache-dir -r /requirements.txt

# Copy your application code
COPY docker2mqtt.py /

# Make the script executable
RUN chmod +x /docker2mqtt.py

# Set the entrypoint
ENTRYPOINT ["/docker2mqtt.py"]
