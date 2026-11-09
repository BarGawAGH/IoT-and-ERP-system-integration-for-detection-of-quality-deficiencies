import time
import datetime
import MySQLdb
import math
import Adafruit_ADS1x15
import csv
import requests
import RPi.GPIO as GPIO
from time import sleep

adc = Adafruit_ADS1x15.ADS1115()


class Database:
    host = '****'
    user = '****'
    password = '****'
    db = "****"
    
    def __init__(self):
        self.connection = MySQLdb.connect(self.host, self.user, self.password, self.db)
        self.cursor = self.connection.cursor()
        
    def insert(self, query):
        try:
            self.cursor.execute(query)
            self.connection.commit()
        except Exception as e:
            print(e)
            
    def query(self, query):
        cursor = self.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            cursor.execute(query)
        except Exception as e:
            print(e)
        return cursor.fetchall()
    
    def __del__(self):
        self.connection.close()
        
def main():
    adc = Adafruit_ADS1x15.ADS1115()

    last_rfrsh = datetime.datetime.now()
    db = None
    now = datetime.datetime.now()

    switch = 25
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(switch, GPIO.OUT)

    GAIN = 1    
    border_value = 2630
    qty = 0
    
    while True:
        value = adc.read_adc(0, gain=GAIN)
        GPIO.output(switch, GPIO.HIGH)

        if value >= border_value:
            qty = qty + 1

            if db == None:
                db = Database()
                last_rfrsh = datetime.datetime.now()

            now = datetime.datetime.now()
            db.insert("INSERT INTO i2c_current.current_motor_71(value, datetime) VALUES (%s, %s);" % (value, "'"+str(now.strftime("%Y-%m-%d %H:%M:%S"))+"'"))
            time.sleep(0.5)
        
        else:    
            if (datetime.datetime.now() - now).total_seconds() > 20:
                qty = 0

        if (datetime.datetime.now() - last_rfrsh).total_seconds() > 300:
            last_rfrsh = datetime.datetime.now()
            del db
            db = None

main()      
            
            
