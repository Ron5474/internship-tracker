import pytest

from db import init_db, make_engine, make_session_factory


@pytest.fixture
def session_factory():
    engine = make_engine(None)
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def session(session_factory):
    s = session_factory()
    yield s
    s.close()
