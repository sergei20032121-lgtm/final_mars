"""Точки расширения под будущего второго агента роя (коптер).

Никакой роевой логики здесь нет — это осознанные заглушки, чтобы модульная
структура была готова принять второго сетевого клиента, когда до него
дойдёт очередь (отдельный, более поздний этап работы). Весь неймспейс
/api/swarm/* требует токен — это единственная точка, где заранее заложена
множественность клиентов, так что граница доверия тут обязательна с
самого начала.
"""
import time

from flask import Blueprint, current_app, jsonify, request

from mars.web.auth import require_token

bp = Blueprint('swarm', __name__)

# In-memory реестр узлов роя. Не переживает перезапуск — для реального
# роя это должно стать частью протокола обнаружения, не хранилищем.
_peers = {}


@bp.route('/api/swarm/report_target', methods=['POST'])
@require_token
def report_target():
    """Второй агент (коптер) сообщает о найденной цели.

    Пока просто принимает и логирует — решение, что с этим делать
    (переключить автопилот в режим GOTO и т.п.), не реализовано.
    """
    state_obj = current_app.state
    data = request.get_json() or {}
    peer_id = data.get('peer_id', 'unknown')
    x = data.get('x')
    y = data.get('y')
    confidence = data.get('confidence')

    if state_obj.logger:
        state_obj.logger.log_event(
            'РОЙ_ЦЕЛЬ',
            f'от {peer_id}: ({x}, {y}) confidence={confidence}'
        )

    return jsonify({'success': True, 'received': {'peer_id': peer_id, 'x': x, 'y': y}})


@bp.route('/api/swarm/peers', methods=['GET'])
@require_token
def list_peers():
    """Список известных узлов роя (регистрируются через POST ниже)."""
    return jsonify({'peers': list(_peers.values())})


@bp.route('/api/swarm/peers', methods=['POST'])
@require_token
def register_peer():
    """Второй агент регистрируется как участник роя."""
    data = request.get_json() or {}
    peer_id = data.get('peer_id')
    if not peer_id:
        return jsonify({'success': False, 'reason': 'peer_id required'}), 400

    _peers[peer_id] = {
        'peer_id': peer_id,
        'kind': data.get('kind', 'unknown'),
        'last_seen': time.time(),
    }
    return jsonify({'success': True})
