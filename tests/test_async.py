#!/usr/bin/env python
import unittest

import psycopg2
from psycopg2 import extensions

import time
import select
import StringIO

import sys
if sys.version_info < (3,):
    import tests
else:
    import py3tests as tests


class PollableStub(object):
    """A 'pollable' wrapper allowing analysis of the `poll()` calls."""
    def __init__(self, pollable):
        self.pollable = pollable
        self.polls = []

    def fileno(self):
        return self.pollable.fileno()

    def poll(self):
        rv = self.pollable.poll()
        self.polls.append(rv)
        return rv


class AsyncTests(unittest.TestCase):

    def setUp(self):
        self.sync_conn = psycopg2.connect(tests.dsn)
        self.conn = psycopg2.connect(tests.dsn, async=True)

        self.wait(self.conn)

        curs = self.conn.cursor()
        curs.execute('''
            CREATE TEMPORARY TABLE table1 (
              id int PRIMARY KEY
            )''')
        self.wait(curs)

    def tearDown(self):
        self.sync_conn.close()
        self.conn.close()

    def wait(self, cur_or_conn):
        pollable = cur_or_conn
        if not hasattr(pollable, 'poll'):
            pollable = cur_or_conn.connection
        while True:
            state = pollable.poll()
            if state == psycopg2.extensions.POLL_OK:
                break
            elif state == psycopg2.extensions.POLL_READ:
                select.select([pollable], [], [])
            elif state == psycopg2.extensions.POLL_WRITE:
                select.select([], [pollable], [])
            else:
                raise Exception("Unexpected result from poll: %r", state)

    def test_connection_setup(self):
        cur = self.conn.cursor()
        sync_cur = self.sync_conn.cursor()

        self.assertEquals(self.conn.issync(), False)
        self.assertEquals(self.sync_conn.issync(), True)

        # the async connection should be in isolevel 0
        self.assertEquals(self.conn.isolation_level, 0)

    def test_async_named_cursor(self):
        self.assertRaises(psycopg2.ProgrammingError,
                          self.conn.cursor, "name")

    def test_async_select(self):
        cur = self.conn.cursor()
        self.assertFalse(self.conn.executing())
        cur.execute("select 'a'")
        self.assertTrue(self.conn.executing())

        self.wait(cur)

        self.assertFalse(self.conn.executing())
        self.assertEquals(cur.fetchone()[0], "a")

    def test_async_callproc(self):
        cur = self.conn.cursor()
        try:
            cur.callproc("pg_sleep", (0.1, ))
        except psycopg2.ProgrammingError:
            # PG <8.1 did not have pg_sleep
            return
        self.assertTrue(self.conn.executing())

        self.wait(cur)
        self.assertFalse(self.conn.executing())
        self.assertEquals(cur.fetchall()[0][0], '')

    def test_async_after_async(self):
        cur = self.conn.cursor()
        cur2 = self.conn.cursor()

        cur.execute("insert into table1 values (1)")

        # an async execute after an async one raises an exception
        self.assertRaises(psycopg2.ProgrammingError,
                          cur.execute, "select * from table1")
        # same for callproc
        self.assertRaises(psycopg2.ProgrammingError,
                          cur.callproc, "version")
        # but after you've waited it should be good
        self.wait(cur)
        cur.execute("select * from table1")
        self.wait(cur)

        self.assertEquals(cur.fetchall()[0][0], 1)

        cur.execute("delete from table1")
        self.wait(cur)

        cur.execute("select * from table1")
        self.wait(cur)

        self.assertEquals(cur.fetchone(), None)

    def test_fetch_after_async(self):
        cur = self.conn.cursor()
        cur.execute("select 'a'")

        # a fetch after an asynchronous query should raise an error
        self.assertRaises(psycopg2.ProgrammingError,
                          cur.fetchall)
        # but after waiting it should work
        self.wait(cur)
        self.assertEquals(cur.fetchall()[0][0], "a")

    def test_rollback_while_async(self):
        cur = self.conn.cursor()

        cur.execute("select 'a'")

        # a rollback should not work in asynchronous mode
        self.assertRaises(psycopg2.ProgrammingError, self.conn.rollback)

    def test_commit_while_async(self):
        cur = self.conn.cursor()

        cur.execute("begin")
        self.wait(cur)

        cur.execute("insert into table1 values (1)")

        # a commit should not work in asynchronous mode
        self.assertRaises(psycopg2.ProgrammingError, self.conn.commit)
        self.assertTrue(self.conn.executing())

        # but a manual commit should
        self.wait(cur)
        cur.execute("commit")
        self.wait(cur)

        cur.execute("select * from table1")
        self.wait(cur)
        self.assertEquals(cur.fetchall()[0][0], 1)

        cur.execute("delete from table1")
        self.wait(cur)

        cur.execute("select * from table1")
        self.wait(cur)
        self.assertEquals(cur.fetchone(), None)

    def test_set_parameters_while_async(self):
        cur = self.conn.cursor()

        cur.execute("select 'c'")
        self.assertTrue(self.conn.executing())

        # getting transaction status works
        self.assertEquals(self.conn.get_transaction_status(),
                          extensions.TRANSACTION_STATUS_ACTIVE)
        self.assertTrue(self.conn.executing())

        # setting connection encoding should fail
        self.assertRaises(psycopg2.ProgrammingError,
                          self.conn.set_client_encoding, "LATIN1")

        # same for transaction isolation
        self.assertRaises(psycopg2.ProgrammingError,
                          self.conn.set_isolation_level, 1)

    def test_reset_while_async(self):
        cur = self.conn.cursor()
        cur.execute("select 'c'")
        self.assertTrue(self.conn.executing())

        # a reset should fail
        self.assertRaises(psycopg2.ProgrammingError, self.conn.reset)

    def test_async_iter(self):
        cur = self.conn.cursor()

        cur.execute("begin")
        self.wait(cur)
        cur.execute("insert into table1 values (1), (2), (3)")
        self.wait(cur)
        cur.execute("select id from table1 order by id")

        # iteration fails if a query is underway
        self.assertRaises(psycopg2.ProgrammingError, list, cur)

        # but after it's done it should work
        self.wait(cur)
        self.assertEquals(list(cur), [(1, ), (2, ), (3, )])
        self.assertFalse(self.conn.executing())

    def test_copy_while_async(self):
        cur = self.conn.cursor()
        cur.execute("select 'a'")

        # copy should fail
        self.assertRaises(psycopg2.ProgrammingError,
                          cur.copy_from,
                          StringIO.StringIO("1\n3\n5\n\\.\n"), "table1")

    def test_lobject_while_async(self):
        # large objects should be prohibited
        self.assertRaises(psycopg2.ProgrammingError,
                          self.conn.lobject)

    def test_async_executemany(self):
        cur = self.conn.cursor()
        self.assertRaises(
            psycopg2.ProgrammingError,
            cur.executemany, "insert into table1 values (%s)", [1, 2, 3])

    def test_async_scroll(self):
        cur = self.conn.cursor()
        cur.execute("insert into table1 values (1), (2), (3)")
        self.wait(cur)
        cur.execute("select id from table1 order by id")

        # scroll should fail if a query is underway
        self.assertRaises(psycopg2.ProgrammingError, cur.scroll, 1)
        self.assertTrue(self.conn.executing())

        # but after it's done it should work
        self.wait(cur)
        cur.scroll(1)
        self.assertEquals(cur.fetchall(), [(2, ), (3, )])

        cur = self.conn.cursor()
        cur.execute("select id from table1 order by id")
        self.wait(cur)

        cur2 = self.conn.cursor()
        self.assertRaises(psycopg2.ProgrammingError, cur2.scroll, 1)

        self.assertRaises(psycopg2.ProgrammingError, cur.scroll, 4)

        cur = self.conn.cursor()
        cur.execute("select id from table1 order by id")
        self.wait(cur)
        cur.scroll(2)
        cur.scroll(-1)
        self.assertEquals(cur.fetchall(), [(2, ), (3, )])

    def test_scroll(self):
        cur = self.sync_conn.cursor()
        cur.execute("create table table1 (id int)")
        cur.execute("insert into table1 values (1), (2), (3)")
        cur.execute("select id from table1 order by id")
        cur.scroll(2)
        cur.scroll(-1)
        self.assertEquals(cur.fetchall(), [(2, ), (3, )])

    def test_async_dont_read_all(self):
        cur = self.conn.cursor()
        cur.execute("select repeat('a', 10000); select repeat('b', 10000)")

        # fetch the result
        self.wait(cur)

        # it should be the result of the second query
        self.assertEquals(cur.fetchone()[0], "b" * 10000)

    def test_async_subclass(self):
        class MyConn(psycopg2.extensions.connection):
            def __init__(self, dsn, async=0):
                psycopg2.extensions.connection.__init__(self, dsn, async=async)

        conn = psycopg2.connect(tests.dsn, connection_factory=MyConn, async=True)
        self.assert_(isinstance(conn, MyConn))
        self.assert_(not conn.issync())
        conn.close()

    def test_flush_on_write(self):
        # a very large query requires a flush loop to be sent to the backend
        curs = self.conn.cursor()
        for mb in 1, 5, 10, 20, 50:
            size = mb * 1024 * 1024
            stub = PollableStub(self.conn)
            curs.execute("select %s;", ('x' * size,))
            self.wait(stub)
            self.assertEqual(size, len(curs.fetchone()[0]))
            if stub.polls.count(psycopg2.extensions.POLL_WRITE) > 1:
                return

        self.fail("sending a large query didn't trigger block on write.")

    def test_sync_poll(self):
        cur = self.sync_conn.cursor()
        cur.execute("select 1")
        # polling with a sync query works
        cur.connection.poll()
        self.assertEquals(cur.fetchone()[0], 1)

    def test_notify(self):
        cur = self.conn.cursor()
        sync_cur = self.sync_conn.cursor()

        sync_cur.execute("listen test_notify")
        self.sync_conn.commit()
        cur.execute("notify test_notify")
        self.wait(cur)

        self.assertEquals(self.sync_conn.notifies, [])

        pid = self.conn.get_backend_pid()
        for _ in range(5):
            self.wait(self.sync_conn)
            if not self.sync_conn.notifies:
                time.sleep(0.5)
                continue
            self.assertEquals(len(self.sync_conn.notifies), 1)
            self.assertEquals(self.sync_conn.notifies.pop(),
                              (pid, "test_notify"))
            return
        self.fail("No NOTIFY in 2.5 seconds")

    def test_async_fetch_wrong_cursor(self):
        cur1 = self.conn.cursor()
        cur2 = self.conn.cursor()
        cur1.execute("select 1")

        self.wait(cur1)
        self.assertFalse(self.conn.executing())
        # fetching from a cursor with no results is an error
        self.assertRaises(psycopg2.ProgrammingError, cur2.fetchone)
        # fetching from the correct cursor works
        self.assertEquals(cur1.fetchone()[0], 1)

    def test_error(self):
        cur = self.conn.cursor()
        cur.execute("insert into table1 values (%s)", (1, ))
        self.wait(cur)
        cur.execute("insert into table1 values (%s)", (1, ))
        # this should fail
        self.assertRaises(psycopg2.IntegrityError, self.wait, cur)
        cur.execute("insert into table1 values (%s); "
                    "insert into table1 values (%s)", (2, 2))
        # this should fail as well
        self.assertRaises(psycopg2.IntegrityError, self.wait, cur)
        # but this should work
        cur.execute("insert into table1 values (%s)", (2, ))
        self.wait(cur)
        # and the cursor should be usable afterwards
        cur.execute("insert into table1 values (%s)", (3, ))
        self.wait(cur)
        cur.execute("select * from table1 order by id")
        self.wait(cur)
        self.assertEquals(cur.fetchall(), [(1, ), (2, ), (3, )])
        cur.execute("delete from table1")
        self.wait(cur)

    def test_error_two_cursors(self):
        cur = self.conn.cursor()
        cur2 = self.conn.cursor()
        cur.execute("select * from no_such_table")
        self.assertRaises(psycopg2.ProgrammingError, self.wait, cur)
        cur2.execute("select 1")
        self.wait(cur2)
        self.assertEquals(cur2.fetchone()[0], 1)

def test_suite():
    return unittest.TestLoader().loadTestsFromName(__name__)

if __name__ == "__main__":
    unittest.main()
