import logging
import time
import asyncio
import abc
from pathlib import Path
import gzip

from streamz import Stream
import attr
import websockets

from ..libs.requesters import Requester

logger = logging.getLogger(__name__)


LIBRARY_NAME = 'numismatic'


@attr.s
class Feed(abc.ABC):
    "Feed Base class"

    rest_client = attr.ib(default=None)
    websocket_client = attr.ib(default=None)
        
    @abc.abstractmethod
    def get_list(self):
        return

    @abc.abstractmethod
    def get_info(self, assets):
        return

    @abc.abstractmethod
    def get_prices(self, assets, currencies):
        return

    def __getattr__(self, attr):
        if self.rest_client is not None and hasattr(self.rest_client, attr):
            return getattr(self.rest_client, attr)
        elif self.websocket_client is not None and hasattr(self.websocket_client, attr):
            return getattr(self.websocket_client, attr)
        else:
            raise AttributeError


@attr.s
class RestClient(abc.ABC):

    cache_dir = attr.ib(default=None)
    requester = attr.ib(default='base')

    @requester.validator
    def __requester_validator(self, attribute, value):
        if isinstance(value, str):
            requester = Requester.factory(value, cache_dir=self.cache_dir)
            setattr(self, attribute.name, requester)
        elif not isinstance(value, Requester):
            raise ValueError(f'{attribute.name}: {value}')

    def _make_request(self, api_url, params=None, headers=None):
        response = self.requester.get(api_url, params=params, headers=headers)
        data = response.json()
        return data


@attr.s
class WebsocketClient(abc.ABC):
    '''Base class for WebsocketClient feeds'''
    # TODO: Write to a separate stream
    output_stream = attr.ib(default=None)
    raw_stream = attr.ib(default=None)
    raw_interval = attr.ib(default=1)

    @classmethod
    async def _connect(cls, wss_url=None):
        if wss_url is None:
            wss_url = cls.wss_url
        logger.info(f'Connecting to {wss_url!r} ...')
        ws = await websockets.connect(wss_url)
        if hasattr(cls, 'on_connect'):
            await cls.on_connect(ws)
        return ws

    @abc.abstractmethod
    async def _subscribe(self, symbol, channel=None, wss_url=None):
        if self.raw_stream is not None:
            # FIXME: Use a FileCollector here
            if self.raw_stream=='':
                from appdirs import user_cache_dir
                self.raw_stream = user_cache_dir(LIBRARY_NAME)
            date = time.strftime('%Y%m%dT%H%M%S')
            filename = f'{self.exchange}_{symbol}_{date}.json.gz'
            raw_stream_path = str(Path(self.raw_stream) / filename)
            logger.info(f'Writing raw stream to {raw_stream_path} ...')

            def write_to_file(batch):
                logger.debug(f'Writing batch of {len(batch)} for {symbol} ...')
                with gzip.open(raw_stream_path, 'at') as f:
                    for packet in batch:
                        f.write(packet+'\n')

            self.raw_stream = Stream()
            (self.raw_stream
             .timed_window(self.raw_interval)
             .filter(len)
             .sink(write_to_file)
             )

        ws = await self._connect(wss_url)
        channel_info = {'channel': channel}
        return ws, channel_info

    @classmethod
    async def _unsubscribe(cls, ws, symbol):
        return True

    async def listen(self, symbol, channel=None, wss_url=None):
        symbol = symbol.upper()
        ws, channel_info = await self._subscribe(symbol,  channel, wss_url)
        while True:
            try:
                packet = await ws.recv()
                msg = self._handle_packet(packet, symbol)
            except asyncio.CancelledError:
                ## unsubscribe
                confirmation = \
                    await asyncio.shield(self._unsubscribe(ws, channel_info))
            except Exception as ex:
                logger.error(ex)
                logger.error(packet)
                raise
             

    @abc.abstractmethod
    def _handle_packet(self, packet, symbol):
        # record the raw packets on the raw_stream
        if self.raw_stream is not None:
            self.raw_stream.emit(packet)
