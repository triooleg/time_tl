import unittest

from time_tl.infrastructure.alerts import InMemorySingletonLease, PostgresAdvisoryLease


class Session:
    def __init__(self):
        self.calls = []

    async def fetch_value(self, sql, *args):
        self.calls.append((sql, args))
        return True


class LeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_in_memory_lease_excludes_second_holder(self):
        lease = InMemorySingletonLease()
        self.assertTrue(await lease.acquire())
        self.assertFalse(await lease.acquire())
        await lease.release()
        self.assertTrue(await lease.acquire())
        await lease.release()

    async def test_postgres_contract_uses_session_advisory_lock(self):
        session = Session()
        lease = PostgresAdvisoryLease(session, 9191)
        self.assertTrue(await lease.acquire())
        self.assertTrue(await lease.renew())
        await lease.release()
        self.assertIn("pg_try_advisory_lock", session.calls[0][0])
        self.assertIn("pg_advisory_unlock", session.calls[1][0])
        self.assertEqual((9191,), session.calls[0][1])


if __name__ == "__main__":
    unittest.main()
