import asyncio
import aio_pika
import logging
import os
from aio_pika import Message


class Pika:

    def __init__(self, host='rabbitmq'):

        self.host = host
        self.server = os.environ['SERVER']
        self.logging = logging.getLogger("rabbitmq")
        self.logging.setLevel(logging.INFO)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(
            logging.Formatter(f'%(asctime)s [RABBITMQ] %(message)s'))
        self.logging.addHandler(ch)
        self.rabbit = None

    async def init(self):
        await self.connect()

        channel = await self.rabbit.channel()
        await channel.set_qos(prefetch_count=1)
        # Incoming
        self.rabbit_queue = await channel.declare_queue(
            'MATCH_HISTORY_IN_' + self.server, durable=True)
        # Outgoing
        self.rabbit_exchange = await channel.declare_exchange(
            f'MATCH_HISTORY_OUT_{self.server}', type='direct',
            durable=True)

        # Output to the Match_History_Updater
        match_in = await channel.declare_queue(
            f'MATCH_IN_{self.server}',
            durable=True
        )
        await match_in.bind(self.rabbit_exchange, 'MATCH')

    async def connect(self):
        time = 0.5
        while not self.rabbit or self.rabbit.is_closed:
            self.rabbit = await aio_pika.connect_robust(
                f'amqp://guest:guest@{self.host}/')
            await asyncio.sleep(time)
            time = min(time + 0.5, 5)
            if time == 5:
                raise ConnectionError("Connection to rabbitmq could not be established.")

    async def get(self):
        return await self.rabbit_queue.get(timeout=1, fail=False)

    async def push(self, data):
        return await self.rabbit_exchange.publish(
            Message(bytes(str(data), 'utf-8')), 'MATCH')
