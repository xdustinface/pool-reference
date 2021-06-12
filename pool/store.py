import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set, List, Tuple, Dict

import aiosqlite
from blspy import G1Element
from chia.pools.pool_wallet_info import PoolState
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_solution import CoinSolution
from chia.util.ints import uint32, uint64

from chia.util.streamable import streamable, Streamable


@dataclass(frozen=True)
@streamable
class FarmerRecord(Streamable):
    launcher_id: bytes32  # This uniquely identifies the singleton on the blockchain (ID for this farmer)
    p2_singleton_puzzle_hash: bytes32  # Derived from the launcher id
    authentication_public_key: G1Element  # This is the latest public key of the farmer (signs all partials)
    singleton_tip: CoinSolution  # Last coin solution that is buried in the blockchain, for this singleton
    singleton_tip_state: PoolState  # Current state of the singleton
    points: uint64  # Total points accumulated since last rest (or payout)
    difficulty: uint64  # Current difficulty for this farmer
    payout_instructions: str  # This is where the pool will pay out rewards to the farmer
    is_pool_member: bool  # If the farmer leaves the pool, this gets set to False


class PoolStore:
    connection: aiosqlite.Connection
    lock: asyncio.Lock

    @classmethod
    async def create(cls):
        self = cls()
        self.db_path = Path("pooldb.sqlite")
        self.connection = await aiosqlite.connect(self.db_path)
        self.lock = asyncio.Lock()
        await self.connection.execute("pragma journal_mode=wal")
        await self.connection.execute("pragma synchronous=2")
        await self.connection.execute(
            (
                "CREATE TABLE IF NOT EXISTS farmer("
                "launcher_id text PRIMARY KEY,"
                " p2_singleton_puzzle_hash text,"
                " authentication_public_key text,"
                " singleton_tip blob,"
                " singleton_tip_state blob,"
                " points bigint,"
                " difficulty bigint,"
                " payout_instructions text,"
                " is_pool_member tinyint)"
            )
        )

        await self.connection.execute(
            "CREATE TABLE IF NOT EXISTS partial(launcher_id text, timestamp bigint, difficulty bigint)"
        )

        await self.connection.execute("CREATE INDEX IF NOT EXISTS scan_ph on farmer(p2_singleton_puzzle_hash)")
        await self.connection.execute("CREATE INDEX IF NOT EXISTS timestamp_index on partial(timestamp)")
        await self.connection.execute("CREATE INDEX IF NOT EXISTS launcher_id_index on partial(launcher_id)")

        await self.connection.commit()

        return self

    @staticmethod
    def _row_to_farmer_record(row) -> FarmerRecord:
        return FarmerRecord(
            bytes.fromhex(row[0]),
            bytes.fromhex(row[1]),
            G1Element.from_bytes(bytes.fromhex(row[2])),
            CoinSolution.from_bytes(row[3]),
            PoolState.from_bytes(row[4]),
            row[5],
            row[6],
            row[7],
            True if row[8] == 1 else False,
        )

    async def add_farmer_record(self, farmer_record: FarmerRecord):
        cursor = await self.connection.execute(
            f"INSERT OR REPLACE INTO farmer VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                farmer_record.launcher_id.hex(),
                farmer_record.p2_singleton_puzzle_hash.hex(),
                bytes(farmer_record.authentication_public_key).hex(),
                bytes(farmer_record.singleton_tip),
                bytes(farmer_record.singleton_tip_state),
                farmer_record.points,
                farmer_record.difficulty,
                farmer_record.payout_instructions,
                int(farmer_record.is_pool_member),
            ),
        )
        await cursor.close()
        await self.connection.commit()

    async def get_farmer_record(self, launcher_id: bytes32) -> Optional[FarmerRecord]:
        # TODO(pool): use cache
        cursor = await self.connection.execute(
            "SELECT * from farmer where launcher_id=?",
            (launcher_id.hex(),),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_farmer_record(row)

    async def update_difficulty(self, launcher_id: bytes32, difficulty: uint64):
        cursor = await self.connection.execute(
            f"UPDATE farmer SET difficulty=? WHERE launcher_id=?", (difficulty, launcher_id.hex())
        )
        await cursor.close()
        await self.connection.commit()

    async def update_singleton(
        self,
        launcher_id: bytes32,
        singleton_tip: CoinSolution,
        singleton_tip_state: PoolState,
        is_pool_member: bool,
    ):
        if is_pool_member:
            entry = (bytes(singleton_tip), bytes(singleton_tip_state), 1, launcher_id)
        else:
            entry = (bytes(singleton_tip), bytes(singleton_tip_state), 0, launcher_id)
        cursor = await self.connection.execute(
            f"UPDATE farmer SET singleton_tip=?, singleton_tip_state=?, is_pool_member=? WHERE launcher_id=?",
            entry,
        )
        await cursor.close()
        await self.connection.commit()

    async def get_pay_to_singleton_phs(self) -> Set[bytes32]:
        cursor = await self.connection.execute("SELECT p2_singleton_puzzle_hash from farmer")
        rows = await cursor.fetchall()

        all_phs: Set[bytes32] = set()
        for row in rows:
            all_phs.add(bytes32(bytes.fromhex(row[0])))
        return all_phs

    async def get_farmer_records_for_p2_singleton_phs(self, puzzle_hashes: Set[bytes32]) -> List[FarmerRecord]:
        if len(puzzle_hashes) == 0:
            return []
        puzzle_hashes_db = tuple([ph.hex() for ph in list(puzzle_hashes)])
        cursor = await self.connection.execute(
            f'SELECT * from farmer WHERE p2_singleton_puzzle_hash in ({"?," * (len(puzzle_hashes_db) - 1)}?) ',
            puzzle_hashes_db,
        )
        rows = await cursor.fetchall()
        return [self._row_to_farmer_record(row) for row in rows]

    async def get_farmer_points_and_payout_instructions(self) -> List[Tuple[uint64, bytes]]:
        cursor = await self.connection.execute(f"SELECT points, payout_instructions from farmer")
        rows = await cursor.fetchall()
        accumulated: Dict[bytes32, uint64] = {}
        for row in rows:
            points: uint64 = uint64(row[0])
            ph: bytes32 = bytes32(bytes.fromhex(row[1]))
            if ph in accumulated:
                accumulated[ph] += points
            else:
                accumulated[ph] = points

        ret: List[Tuple[uint64, bytes32]] = []
        for ph, total_points in accumulated.items():
            ret.append((total_points, ph))
        return ret

    async def clear_farmer_points(self) -> None:
        cursor = await self.connection.execute(f"UPDATE farmer set points=0")
        await cursor.close()
        await self.connection.commit()

    async def add_partial(self, launcher_id: bytes32, timestamp: uint64, difficulty: uint64):
        cursor = await self.connection.execute(
            "INSERT into partial VALUES(?, ?, ?)",
            (launcher_id.hex(), timestamp, difficulty),
        )
        await cursor.close()
        await self.connection.commit()

    async def get_recent_partials(self, launcher_id: bytes32, count: int) -> List[Tuple[uint64, uint64]]:
        cursor = await self.connection.execute(
            "SELECT timestamp, difficulty from partial WHERE launcher_id=? ORDER BY timestamp DESC LIMIT ?",
            (launcher_id.hex(), count),
        )
        rows = await cursor.fetchall()
        ret: List[Tuple[uint64, uint64]] = [(uint64(timestamp), uint64(difficulty)) for timestamp, difficulty in rows]
        return ret
