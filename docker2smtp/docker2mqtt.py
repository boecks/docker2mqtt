#!/usr/bin/env python3
"""Listens to Docker events and sends container stop/start events to MQTT using docker-py."""
import atexit
import json
import logging
import re
import signal
from os import environ
from socket import gethostname
import paho.mqtt.client as mqtt
import docker
from time import sleep, time
from docker.errors import ImageNotFound

# --- Configuration & Constants ---
DEBUG = environ.get('DEBUG', '1') == '1'
DESTROYED_CONTAINER_TTL = int(environ.get('DESTROYED_CONTAINER_TTL', 24 * 60 * 60))
HOMEASSISTANT_PREFIX = environ.get('HOMEASSISTANT_PREFIX', 'homeassistant')
DOCKER2MQTT_HOSTNAME = environ.get('DOCKER2MQTT_HOSTNAME', gethostname())
MQTT_CLIENT_ID = environ.get('MQTT_CLIENT_ID', 'docker2mqtt')
MQTT_USER = environ.get('MQTT_USER', '')
MQTT_PASSWD = environ.get('MQTT_PASSWD', '')
MQTT_HOST = environ.get('MQTT_HOST', 'localhost')
MQTT_PORT = int(environ.get('MQTT_PORT', '1883'))
MQTT_TIMEOUT = int(environ.get('MQTT_TIMEOUT', '30'))
MQTT_TOPIC_PREFIX = environ.get('MQTT_TOPIC_PREFIX', 'docker')
MQTT_QOS = int(environ.get('MQTT_QOS', 1))
DISCOVERY_TOPIC = f'{HOMEASSISTANT_PREFIX}/binary_sensor/{MQTT_TOPIC_PREFIX}/{DOCKER2MQTT_HOSTNAME}_{{}}/config'
WATCHED_EVENTS = ('create', 'destroy', 'die', 'pause', 'rename', 'start', 'stop', 'unpause')

# --- Logging Setup ---
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

# --- Global Variables ---
known_containers = {}
pending_destroy_operations = {}
invalid_ha_topic_chars = re.compile(r'[^a-zA-Z0-9_]')

# --- Functions ---

def mqtt_disconnect(mqtt_client):
    """Called by atexit to send the last_will message and disconnect MQTT."""
    logger.info('Disconnecting MQTT client.')
    mqtt_client.publish(f'{MQTT_TOPIC_PREFIX}/{DOCKER2MQTT_HOSTNAME}/status', 'offline', qos=MQTT_QOS, retain=True)
    mqtt_client.disconnect()
    sleep(1)
    mqtt_client.loop_stop()

def mqtt_send(mqtt_client, topic: str, payload: str, retain=False):
    """Send payload to the specified MQTT topic."""
    try:
        logger.debug(f'Sending to MQTT: {topic}: {payload}')
        mqtt_client.publish(topic, payload=payload, qos=MQTT_QOS, retain=retain)
    except Exception as e:
        logger.error(f'MQTT Publish Failed: {e}')

def register_container(mqtt_client, container_entry: dict):
    """Register a new container with Home Assistant."""
    known_containers[container_entry['name']] = container_entry
    registration_topic = DISCOVERY_TOPIC.format(invalid_ha_topic_chars.sub('_', container_entry['name']))
    registration_packet = {
        'name': f"{MQTT_TOPIC_PREFIX.title()} {container_entry['name']}",
        'unique_id': f'{MQTT_TOPIC_PREFIX}_{DOCKER2MQTT_HOSTNAME}_{registration_topic}',
        'availability_topic': f'{MQTT_TOPIC_PREFIX}/{DOCKER2MQTT_HOSTNAME}/status',
        'payload_available': 'online',
        'payload_not_available': 'offline',
        'state_topic': f'{MQTT_TOPIC_PREFIX}/{DOCKER2MQTT_HOSTNAME}/{container_entry["name"]}',
        'value_template': '{{ value_json.state }}',
        'payload_on': 'on',
        'payload_off': 'off',
        'device_class': 'connectivity',
        'json_attributes_topic': f'{MQTT_TOPIC_PREFIX}/{DOCKER2MQTT_HOSTNAME}/{container_entry["name"]}',
    }
    mqtt_send(mqtt_client, registration_topic, json.dumps(registration_packet), retain=True)
    mqtt_send(mqtt_client, f'{MQTT_TOPIC_PREFIX}/{DOCKER2MQTT_HOSTNAME}/{container_entry["name"]}', json.dumps(container_entry), retain=True)

def parse_docker_ps(docker_client):
    """Fetches and parses all Docker containers using docker-py."""
    containers = []
    for container in docker_client.containers.list(all=True):
        try:
            image_tags = container.image.tags[0] if container.image.tags else container.image.id
        except ImageNotFound:
            image_tags = 'unknown'
        status_str, state_str = ('running', 'on') if container.status == 'running' else ('paused', 'off') if container.status == 'paused' else ('stopped', 'off')
        containers.append({
            'name': container.name,
            'image': image_tags,
            'status': status_str,
            'state': state_str
        })
    return containers

def listen_to_docker_events(docker_client, mqtt_client):
    """Listen to Docker events and process container events."""
    try:
        for event in docker_client.events(decode=True):
            container_name = event.get('Actor', {}).get('Attributes', {}).get('name', '')
            if event.get('status') not in WATCHED_EVENTS or not container_name:
                continue

            if event['status'] == 'create':
                logger.info(f'Container {container_name} has been created.')
                register_container(mqtt_client, {
                    'name': container_name,
                    'image': event.get('from', ''),
                    'status': 'created',
                    'state': 'off'
                })

            elif event['status'] == 'destroy':
                logger.info(f'Container {container_name} has been destroyed.')
                pending_destroy_operations[container_name] = time()
                known_containers[container_name]['status'] = 'destroyed'
                known_containers[container_name]['state'] = 'off'

            elif event['status'] == 'die':
                logger.info(f'Container {container_name} has stopped.')
                known_containers[container_name]['status'] = 'stopped'
                known_containers[container_name]['state'] = 'off'

            elif event['status'] == 'pause':
                logger.info(f'Container {container_name} has paused.')
                known_containers[container_name]['status'] = 'paused'
                known_containers[container_name]['state'] = 'off'

            elif event['status'] == 'rename':
                old_name = event['Actor']['Attributes']['oldName'].lstrip('/')
                logger.info(f'Container {old_name} renamed to {container_name}.')
                mqtt_send(mqtt_client, f'{HOMEASSISTANT_PREFIX}/binary_sensor/{MQTT_TOPIC_PREFIX}/{old_name}/config', '', retain=True)
                mqtt_send(mqtt_client, f'{MQTT_TOPIC_PREFIX}/{DOCKER2MQTT_HOSTNAME}/{old_name}', '', retain=True)
                register_container(mqtt_client, {
                    'name': container_name,
                    'image': known_containers[old_name]['image'],
                    'status': known_containers[old_name]['status'],
                    'state': known_containers[old_name]['state']
                })
                del(known_containers[old_name])

            elif event['status'] == 'start':
                logger.info(f'Container {container_name} has started.')
                known_containers[container_name]['status'] = 'running'
                known_containers[container_name]['state'] = 'on'

            elif event['status'] == 'unpause':
                logger.info(f'Container {container_name} has unpaused.')
                known_containers[container_name]['status'] = 'running'
                known_containers[container_name]['state'] = 'on'

            mqtt_send(mqtt_client, f'{MQTT_TOPIC_PREFIX}/{DOCKER2MQTT_HOSTNAME}/{container_name}', json.dumps(known_containers[container_name]), retain=True)
    except (KeyboardInterrupt, SystemExit):
        logger.info('Interrupt received. Shutting down gracefully.')
        mqtt_disconnect(mqtt_client)
        raise

def signal_handler(sig, frame, mqtt_client):
    """Handle signals and perform graceful shutdown."""
    logger.info(f'Signal {sig} received. Shutting down gracefully.')
    mqtt_disconnect(mqtt_client)
    exit(0)

def main():
    # Setup Docker client
    docker_client = docker.from_env()

    # Setup MQTT
    mqtt_client = mqtt.Client(client_id=MQTT_CLIENT_ID, protocol=mqtt.MQTTv311)
    mqtt_client.username_pw_set(username=MQTT_USER, password=MQTT_PASSWD)
    mqtt_client.will_set(f'{MQTT_TOPIC_PREFIX}/{DOCKER2MQTT_HOSTNAME}/status', 'offline', qos=MQTT_QOS, retain=True)
    mqtt_client.connect(MQTT_HOST, MQTT_PORT, MQTT_TIMEOUT)
    mqtt_client.loop_start()

    # Register atexit handler
    atexit.register(mqtt_disconnect, mqtt_client)

    # Register signal handlers
    signal.signal(signal.SIGINT, lambda s, f: signal_handler(s, f, mqtt_client))
    signal.signal(signal.SIGTERM, lambda s, f: signal_handler(s, f, mqtt_client))
    signal.signal(signal.SIGHUP, lambda s, f: signal_handler(s, f, mqtt_client))

    # Send "online" status at startup
    mqtt_send(mqtt_client, f'{MQTT_TOPIC_PREFIX}/{DOCKER2MQTT_HOSTNAME}/status', 'online', retain=True)

    # Register existing containers with HA
    for container in parse_docker_ps(docker_client):
        register_container(mqtt_client, container)

    # Listen to Docker events
    listen_to_docker_events(docker_client, mqtt_client)

if __name__ == '__main__':
    main()
