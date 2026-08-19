"""create_app() в sim_mode (GPIO недоступен на этой машине) + Flask test client
бьёт ключевые роуты. Не поднимает реальный сервер, не трогает GPIO/камеру."""
import pytest

from mars.app import create_app


@pytest.fixture(scope="module")
def client():
    app = create_app()
    app.testing = True
    with app.test_client() as c:
        yield c


@pytest.fixture(scope="module")
def token(client):
    return client.application.config['MARS_TOKEN']


def test_index_renders(client):
    r = client.get('/')
    assert r.status_code == 200
    assert b'\xd0\x9c.\xd0\x90.\xd0\xa0.\xd0\xa1.' in r.data or 'М.А.Р.С.'.encode() in r.data


def test_robot_state(client):
    r = client.get('/api/robot/state')
    assert r.status_code == 200
    data = r.get_json()
    assert 'state' in data
    assert 'x' in data['state']
    assert data['state']['gpio_enabled'] is False  # на этой машине GPIO недоступен -> sim


def test_robot_command_requires_token(client):
    r = client.post('/api/robot/command', json={'cmd': 'forward'})
    assert r.status_code == 401


def test_robot_command_with_token(client, token):
    r = client.post('/api/robot/command', json={'cmd': 'stop'},
                     headers={'X-MARS-Token': token})
    assert r.status_code == 200
    assert r.get_json()['success'] is True


def test_estop_roundtrip(client, token):
    r = client.post('/api/robot/estop', json={'activate': True},
                     headers={'X-MARS-Token': token})
    assert r.status_code == 200
    assert r.get_json()['estop'] is True

    r = client.get('/api/robot/estop/status')
    assert r.get_json()['estop'] is True

    r = client.post('/api/robot/estop', json={'activate': False},
                     headers={'X-MARS-Token': token})
    assert r.get_json()['estop'] is False


def test_sonar_status(client):
    r = client.get('/api/sonar')
    assert r.status_code == 200
    assert 'distance_cm' in r.get_json()


def test_stats(client):
    r = client.get('/api/stats')
    assert r.status_code == 200


def test_map_jpeg(client):
    r = client.get('/api/map')
    assert r.status_code == 200
    assert r.mimetype == 'image/jpeg'


def test_camera_frame_placeholder(client):
    r = client.get('/api/camera/frame')
    assert r.status_code == 200
    assert r.mimetype == 'image/jpeg'


def test_swarm_requires_token(client):
    r = client.get('/api/swarm/peers')
    assert r.status_code == 401


def test_swarm_report_target(client, token):
    r = client.post('/api/swarm/report_target',
                     json={'peer_id': 'copter-1', 'x': 100, 'y': 200, 'confidence': 0.9},
                     headers={'X-MARS-Token': token})
    assert r.status_code == 200
    assert r.get_json()['success'] is True
