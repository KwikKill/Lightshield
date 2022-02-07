import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

import aiohttp

from lightshield.exceptions import (
    LimitBlocked,
    RatelimitException,
    NotFoundException,
    Non200Exception,
)


class Platform:
    running = False
    _runner = None

    def __init__(self, region, platforms, handler):
        self.name = region
        self.platforms = platforms
        self.handler = handler
        self.logging = logging.getLogger("%s" % region)
        self.tasks = []  # List of summonerIds
        self.results = []  # Properly returning entries.
        self.result_not_found = []
        self._worker = []  # Worker promises
        self.updater = None  # Updater promise
        self.retry_after = datetime.now()
        self.proxy = handler.proxy
        self.endpoint_url = (
            f"https://{self.name}.api.riotgames.com/lol/match/v5/matches/%s_%s"
        )

    async def shutdown(self):
        self.logging.info("Shutdown")
        for worker in self._worker:
            worker.cancel()
        self.updater.cancel()
        await self._runner

    async def start(self):
        """Start the service calls."""
        if not self.running:
            self.logging.info("Started service calls.")
        self.running = True

    async def stop(self):
        """Halt the service calls."""
        if self.running:
            self.logging.info("Stopped service calls.")
        self.running = False

    async def init(self):
        """Init background runner."""
        self.endpoint = await self.handler.proxy.get_endpoint(
            server=self.name, zone="match-details-v5"
        )
        self._runner = asyncio.create_task(self.runner())

    async def runner(self):
        """Main object loop."""
        self.updater = asyncio.create_task(self.task_updater())
        self._worker = [asyncio.create_task(self.worker()) for _ in range(10)]
        try:
            await asyncio.gather(*self._worker, self.updater)
        except asyncio.CancelledError:
            await self.flush_tasks(self.results, self.result_not_found)
            return

    async def task_updater(self):
        """Pull new tasks when the list is empty."""
        while True:
            if len(self.result_matchupdates) >= 100:
                data = [
                    self.results.copy(),
                    self.result_not_found.copy(),
                ]
                self.results = []
                self.result_not_found = []
                await self.flush_tasks(*data)

            if not self.running:
                await asyncio.sleep(5)
                continue
            if len(self.tasks) > 200:
                await asyncio.sleep(5)
                continue
            try:
                async with self.handler.postgres.acquire() as connection:
                    entries = await connection.fetch(
                        """UPDATE %s.match
                                SET reserved_details = current_date + INTERVAL '10 minute'
                                FROM (
                                    SELECT  match_id,
                                            platform
                                        FROM %s.match
                                        WHERE details IS NULL
                                        AND find_fails <= 10
                                        AND (reserved_details IS NULL OR reserved_details < current_timestamp)
                                        ORDER BY find_fails, match_id DESC
                                        LIMIT $1
                                        ) selection
                            WHERE match.match_id = selection.match_id
                               AND match.platform = selection.platform
                                RETURNING match.platform, match.match_id
                        """
                        % tuple([self.name for _ in range(2)]),
                        500,
                    )
                    self.logging.debug(
                        "Refilling tasks [%s -> %s].",
                        len(self.tasks),
                        len(self.tasks) + len(entries),
                    )
                    if len(entries) == 0:
                        await asyncio.sleep(30)
                        data = [
                            self.results.copy(),
                            self.result_not_found.copy(),
                        ]
                        self.results = []
                        self.result_not_found = []
                        await self.flush_tasks(*data)

                    self.tasks += entries
            except Exception as err:
                self.logging.error("Here: %s", err)

    async def fetch(self, params, session):
        """Call and handle response."""
        try:
            #   Success
            url = self.endpoint_url % (params["platform"], params["match_id"])
            response = await self.endpoint.request(url, session)
            if response["info"]["queueId"] == 0:
                raise NotFoundException  # TODO: queue 0 means its a custom, so it should be set to max retries immediatly

            # Extract relevant data
            queue = response["info"]["queueId"]
            creation = datetime.fromtimestamp(response["info"]["gameCreation"] // 1000)
            patch = ".".join(response["info"]["gameVersion"].split(".")[:2])
            if "gameStartTimestamp" in response["info"]:
                game_duration = (
                    response["info"]["gameStartTimestamp"]
                    - response["info"]["gameStartTimestamp"]
                )
            else:
                game_duration = response["info"]["gameDuration"]
            if game_duration >= 30000:
                game_duration //= game_duration
            win = [
                team["win"]
                for team in response["info"]["teams"]
                if team["teamId"] == 100
            ][0]
            players = []
            for player in response["info"]["participants"]:
                players.append(
                    [
                        params["platform"],
                        params["match_id"],
                        player["puuid"],
                        player["teamId"] != 100,
                        player["championId"] if player["championId"] < 30000 else -1
                        # TODO: Add lane: 'lane': player['teamPosition'], (add as a enum in postgres first)
                    ]
                )
            path = os.path.join(os.sep, "data", "details", patch, params["platform"])
            os.makedirs(path)
            with open(os.path.join(path, "%s_%s.json" % params), "w+") as file:
                json.dump(response, file)
            response = None
            self.logging.debug(url)
            package = {
                "match": [
                    queue,
                    creation,
                    game_duration,
                    win,
                    params["platform"],
                    params["match_id"],
                ],
                "participant": players,
            }
            self.results.append(package)
        except LimitBlocked as err:
            self.retry_after = datetime.now() + timedelta(seconds=err.retry_after)
            return params
        except RatelimitException as err:
            self.logging.error("Ratelimit")
            return params
        except Non200Exception as err:
            self.logging.error("Others")
            return params
        except NotFoundException:
            self.result_not_found.append([params["platform"], params["match_id"]])
        except Exception as err:
            self.logging.error("General: %s", err)
            return params

    async def worker(self):
        """Execute requests."""
        targets = []
        while not self.running:
            await asyncio.sleep(5)
        while True:
            if not self.running:
                await asyncio.sleep(5)
                continue
            if not self.tasks:
                await asyncio.sleep(5)
                continue
            while len(targets) < 5:
                if not self.tasks:
                    break
                targets.append(self.tasks.pop())
            async with aiohttp.ClientSession(
                headers={"X-Riot-Token": self.handler.api_key}
            ) as session:
                targets = [
                    target
                    for target in (
                        await asyncio.gather(
                            *[
                                asyncio.create_task(self.fetch(target, session))
                                for target in targets
                            ]
                        )
                    )
                    if target
                ]

    async def flush_tasks(self, match_updates, match_not_found):
        """Insert results from requests into the db."""
        try:
            async with self.handler.postgres.acquire() as connection:
                if match_updates or match_not_found:
                    self.logging.info(
                        "Flushing %s match_updates (%s not found).",
                        len(match_updates) + len(match_not_found),
                        len(match_not_found),
                    )
                async with connection.transaction():
                    if match_updates:
                        matches = [package["match"] for package in match_updates]
                        # Insert match updates
                        query = await connection.prepare(
                            """UPDATE %s.match
                            SET queue = $1,
                                timestamp = $2,
                                duration = $3,
                                win = $4,
                                details = TRUE,
                                reserved_details = NULL
                                WHERE platform = $5
                                AND match_id = $6
                            """
                            % self.name,
                        )
                        await query.executemany(matches)
                        await connection.executemany()

                        participants = []
                        for package in match_updates:
                            participants += package["participant"]
                        await connection.executemany(
                            """INSERT INTO $1.participant
                                VALUES ($2, $3, $4, $5)
                                ON CONFLICT DO NOTHING
                            """,
                            participants,
                        )

                    if match_not_found:
                        query = await connection.prepare(
                            """UPDATE %s.match
                                SET find_fails = find_fails + 1,
                                    reserved_details = current_date + INTERVAL '10 minute'
                                WHERE platform = $1
                                AND match_id = $2
                            """
                            % self.name
                        )
                        await query.executemany(match_not_found)

        except Exception as err:
            self.logging.info(err)
