import paho.mqtt.client as mqtt
import json
import time

MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
MQTT_TOPIC = "westavia/telemetry/test_drone_001"

mqtt_connected = False
last_mqtt_recv_time = 0.0  

def start_mqtt_client(socketio, process_callback):
    def on_connect(client, userdata, flags, rc):
        global mqtt_connected
        if rc == 0:
            print("[MQTT] Connected to Cloud Broker!")
            mqtt_connected = True
            # මෙන්න මේ පේළිය තමයි කලින් මගේ අතින් මැකිලා තිබුණේ!
            client.subscribe(MQTT_TOPIC)
        else:
            mqtt_connected = False

    def on_disconnect(client, userdata, rc):
        global mqtt_connected
        mqtt_connected = False

    def on_message(client, userdata, msg):
        global last_mqtt_recv_time
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            payload['source'] = 'Network RID'
            
            last_mqtt_recv_time = time.time()  
            process_callback(payload)
        except Exception as e:
            print(f"[!] Error parsing MQTT data: {e}")

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()