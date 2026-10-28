"""验证样例快照和核心逻辑：python verify_market_slice.py。"""
import json
import tempfile
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from market_slice import MarketReplay, local_time, read_data


def verify_snapshots(replay, folder):
    book = replay.book_at(0, 0, include_auction=False)
    replay.apply(book, replay.auction_changes)
    no = nt = 0
    rows = []
    snapshots = replay.snapshots[replay.snapshots.timestamp >= replay.trades.timestamp.iloc[0]]
    for snap in snapshots.itertuples():
        ntrade = int(snap.num_trades) - replay.initial_count
        if not 0 <= ntrade <= len(replay.trades):
            raise AssertionError("快照成交笔数超出逐笔文件范围")
        replay.apply(book, replay.trade_events[nt:ntrade])
        nt = ntrade
        # 用总挂单量扣除被动成交量，定位独立委托流的消息边界。
        key = (int(snap.total_bid_vol) + int(replay.passive['buy'][nt]) + replay.initial_volume,
               int(snap.total_ask_vol) + int(replay.passive['sell'][nt]) + replay.initial_volume)
        expected = {side: [{"price": float(getattr(snap, f"{p}p{i}")),
                           "quantity": int(getattr(snap, f"{p}v{i}"))} for i in range(1, 11)]
                    for side, p in [('buy', 'b'), ('sell', 'a')]}
        candidates = replay.order_lookup.get(key, [])
        matched = False
        tried = 0
        for norder in candidates:
            if norder < no:
                continue
            replay.apply(book, replay.order_events[no:norder])
            no = norder
            tried += 1
            # 总量相同可能对应撤单后重新挂单，必须用档位区分这些候选边界。
            matched = all(replay.top10(book, side) == expected[side] for side in expected)
            if matched:
                break
        volume_error = replay.initial_volume + int(replay.cum_volume[nt]) - int(snap.volume)
        amount_error = replay.initial_amount + float(replay.cum_amount[nt]) - float(snap.amount) * replay.scale
        last = float(replay.trades.price.iloc[nt-1]) if nt else float(replay.auction['last'])
        row = {"snapshot_time": snap.timestamp.isoformat(), "matched_levels": 20 if matched else 0,
               "volume_error": volume_error, "amount_rounding_error_yuan": round(amount_error, 6),
               "last_price_matches": last == float(snap.last), "candidate_boundaries_tried": tried}
        rows.append(row)
        assert matched, f"盘口不匹配：{snap.timestamp}"
        # 收盘后快照的 last 是行情给出的收盘价，可能不同于最后一笔成交价。
        is_end = "E" in str(getattr(snap, 'trading_phase_code', ''))
        assert volume_error == 0 and (row['last_price_matches'] or is_end), f"成交数据不匹配：{snap.timestamp}"
        assert abs(amount_error) <= replay.scale * 0.00005 + 0.01
        assert all(q >= 0 for levels in book.values() for q in levels.values())
        # 直接检查生产 query()。快照边界之后到下一整秒的事件，由参考簿独立累加。
        target = max(snap.timestamp, replay.orders.timestamp.iloc[no-1] if no else snap.timestamp,
                     replay.trades.timestamp.iloc[nt-1] if nt else snap.timestamp).ceil('s')
        qo = int(replay.orders.timestamp.searchsorted(target, side='right'))
        qt = int(replay.trades.timestamp.searchsorted(target, side='right'))
        reference = {s: defaultdict(int, levels) for s, levels in book.items()}
        replay.apply(reference, replay.order_events[no:qo])
        replay.apply(reference, replay.trade_events[nt:qt])
        actual = replay.query(target)
        assert actual['bids'] == replay.top10(reference, 'buy'), f'query买盘错误：{target}'
        assert actual['asks'] == replay.top10(reference, 'sell'), f'query卖盘错误：{target}'
        assert actual['volume'] == int(snap.volume) + int(replay.cum_volume[qt] - replay.cum_volume[nt])
        assert actual['num_trades'] == int(snap.num_trades) + qt - nt
        assert actual['total_bid_vol'] == sum(reference['buy'].values())
        assert actual['total_ask_vol'] == sum(reference['sell'].values())
        row['query_time'] = target.isoformat()
        row['query_verified'] = True
        if len(rows) % 1000 == 0:
            print(f'已核对 {len(rows)} 张快照及实际query输出', flush=True)
    report = pd.DataFrame(rows)
    report.to_csv(folder / '盘口修正验证.csv', index=False, encoding='utf-8-sig')
    print(f"快照对齐及实际query验证：{len(report)} 张快照，买卖 {len(report)*20:,} 个档位全部匹配")
    print("边界由笔数/总委托量校准；有多个候选时用十档区分，并非整秒直接比较。")


def verify_small_cases():
    """独立的小样例，覆盖扣减方向、撤单、深档补位、时间边界与数据清洗。"""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        snap = {k: 0 for k in ['last', 'pre_close', 'open', 'high', 'low', 'volume', 'amount',
                              'num_trades', 'total_bid_vol', 'total_ask_vol']}
        snap.update({f'{p}{kind}{i}': 0 for p in 'ab' for kind in 'pv' for i in range(1, 11)})
        snap.update(time=92959000, date=20240207, symbol='SH.TEST', pre_close=10)
        pd.DataFrame([snap, snap, dict(snap, time=250000000), dict(snap, last=None)]).to_csv(
            folder / 'df_level2.csv', index=False)
        orders = []
        for i in range(11):
            orders.append(dict(date=20240207, symbol='SH.TEST', channel=1, index=i+1,
                order_time='2024-02-07 09:30:00+08:00', side='buy', price=10-i/10,
                quantity=100, order_type='place'))
        orders += [dict(orders[0], index=12, side='sell', price=11, quantity=300),
                   dict(orders[0], index=13, order_type='cancel',
                        order_time='2024-02-07 09:30:01+08:00')]
        bad = [dict(orders[0], index=14, quantity=-1), dict(orders[0], index=15, price=None),
               dict(orders[0], index=16, order_time='bad-time')]
        pd.DataFrame(orders + [orders[0]] + bad).to_csv(folder / 'df_order.csv', index=False)
        trade = dict(date=20240207, symbol='SH.TEST', channel=1, index=1, price=11,
            quantity=50, trade_time='2024-02-07 09:30:01+08:00', other_info='external(buy)')
        pd.DataFrame([trade, trade]).to_csv(folder / 'df_trade.csv', index=False)
        replay = MarketReplay(folder, '2024-02-07', 'SH.TEST', 1)
        before = replay.query('2024-02-07 09:30:00')
        after = replay.query('2024-02-07 09:30:01')
        assert before['volume'] == 0 and before['bids'][0]['quantity'] == 100
        assert after['asks'][0] == {'price': 11., 'quantity': 250}
        assert after['bids'][0]['price'] == 9.9 and after['bids'][9]['price'] == 9.
        assert after['total_bid_vol'] == 1000 and after['total_ask_vol'] == 250
        assert after['volume'] == 50 and after['amount'] == 550 and after['num_trades'] == 1
        assert after['warnings'] == [] and after['cleaned_rows']['df_order.csv'] == 4
        assert after['cleaned_rows']['df_trade.csv'] == 1
        assert after['cleaned_rows']['df_level2.csv'] == 3
        assert replay.query('2024-02-07T01:30:01Z') == after
        # 同一个有效订单不能因重复行被累加两次。
        assert len(read_data(folder, 'df_order.csv', 'order_time')) == 13
        pd.DataFrame([dict(trade, other_info='unknown')]).to_csv(folder / 'df_trade.csv', index=False)
        try:
            MarketReplay(folder, '2024-02-07', 'SH.TEST', 1)
        except ValueError as exc:
            assert '内外盘' in str(exc)
        else:
            raise AssertionError('未知内外盘标志不能猜测扣减方向')
        print('独立小样例：被动侧扣减、撤单、十一档补位、去重/缺失/异常清洗、时区及时间边界通过')


def verify_correction_and_missing(replay):
    """回归审核发现的问题：快照修正真实 query，缺失委托不再静默产生错误。"""
    cache = dict(zip(['df_level2.csv', 'df_order.csv', 'df_trade.csv'],
                     [replay.snapshots, replay.orders, replay.trades]))
    orders = replay.orders
    ix = orders[(orders.timestamp >= local_time('2024-02-07 09:30:00'))
                & (orders.timestamp < local_time('2024-02-07 09:30:04'))
                & orders.side.eq('buy') & orders.order_type.eq('place')
                & orders.price.eq(2.351) & orders.quantity.eq(100)].index[0]
    def missing_order(folder, name, col):
        df = cache[name].copy()
        if name == 'df_order.csv':
            df = df.drop(index=ix).reset_index(drop=True)
        df.attrs['removed'] = int(name == 'df_order.csv')
        return df
    with patch('market_slice.read_data', side_effect=missing_order):
        damaged = MarketReplay('.', '2024-02-07')
        try:
            damaged.query('2024-02-07 09:30:05')
        except ValueError as exc:
            assert '委托' in str(exc) or '盘口' in str(exc)
        else:
            raise AssertionError('不能继续返回少100的买一数量')


def verify_synthetic_correction(replay):
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        snapshot = replay.snapshots.iloc[0].drop(labels='timestamp').to_dict()
        snapshot.update(symbol='SH.TEST', time=92959000, volume=0, amount=0, num_trades=0,
                        last=0, open=0, high=0, low=0, total_bid_vol=0, total_ask_vol=0)
        snapshot.update({f'{side}{kind}{i}': 0 for side in 'ab' for kind in 'pv' for i in range(1, 11)})
        corrected = dict(snapshot, time=93001000, total_bid_vol=200, total_ask_vol=300,
                         bp1=10, bv1=100, bp2=9, bv2=100, ap1=11, av1=300)
        pd.DataFrame([snapshot, corrected]).to_csv(p / 'df_level2.csv', index=False)
        order = dict(date=20240207, symbol='SH.TEST', channel=1, order_type='place',
                     order_time='2024-02-07 09:30:00', quantity=100, side='buy', index=1, price=9.5)
        # 第一笔挂单的价格与权威快照不符，但总量和消息边界唯一，可以修正。
        pd.DataFrame([order, dict(order, index=2, price=9),
                      dict(order, index=3, side='sell', price=11, quantity=300)]).to_csv(p / 'df_order.csv', index=False)
        trade = dict(date=20240207, symbol='SH.TEST', channel=1, index=1, price=11, quantity=50,
                     trade_time='2024-02-07 09:30:02', other_info='external(buy)')
        pd.DataFrame([trade]).to_csv(p / 'df_trade.csv', index=False)
        result = MarketReplay(p, '2024-02-07', 'SH.TEST', 1).query('2024-02-07 09:30:02')
        assert result['snapshot_corrected'] is True
        assert result['bids'][:2] == [{'price': 10., 'quantity': 100}, {'price': 9., 'quantity': 100}]
        assert result['asks'][0] == {'price': 11., 'quantity': 250}
        assert result['volume'] == 50 and result['amount'] == 550
    print('合成快照校正和北京时间检查通过')


if __name__ == '__main__':
    folder = Path(__file__).parent
    verify_small_cases()
    replay = MarketReplay(folder, '2024-02-07')
    verify_correction_and_missing(replay)
    verify_synthetic_correction(replay)
    verify_snapshots(replay, folder)
    result = replay.query('2024-02-07 09:30:05')
    assert result['volume'] == 14717000 and result['num_trades'] == 837
    assert result['bids'][0]['quantity'] == 2017500
    assert result['asks'][0] == {'price': 2.353, 'quantity': 3700}
    assert replay.query('2024-02-07 09:30:04')['snapshot_time'].endswith('09:30:01+08:00')
    # 删除目标之后的所有行，查询结果仍必须相同，证明没有用未来数据补盘口。
    cutoff = local_time('2024-02-07 09:30:05')
    cache = dict(zip(['df_level2.csv', 'df_order.csv', 'df_trade.csv'],
                     [replay.snapshots, replay.orders, replay.trades]))
    def past_only(folder, name, col):
        df = cache[name][cache[name].timestamp <= cutoff].copy()
        df.attrs['removed'] = 0
        return df
    with patch('market_slice.read_data', side_effect=past_only):
        assert MarketReplay(folder, '2024-02-07').query(cutoff) == result
    (folder / '盘口示例.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    for value in ['2024-02-07 09:15:05', '2024-02-07 09:25:05',
                  '2024-02-07 09:30:04', '2024-02-07 12:00:00', '2024-02-07 15:10:00']:
        r = replay.query(value)
        assert len(r['bids']) == len(r['asks']) == 10
        json.dumps(r, allow_nan=False)
    last_volume = 0
    for number, value in enumerate(pd.date_range('2024-02-07 09:30:00', '2024-02-07 15:00:00', freq='s'), 1):
        r = replay.query(value)
        assert r['warnings'] == [] and r['volume'] >= last_volume
        last_volume = r['volume']
        if number % 5000 == 0:
            print(f'实际query整秒检查：{number}/19801', flush=True)
    try:
        replay.query('2025-01-01 09:30:05')
    except ValueError:
        pass
    else:
        raise AssertionError('缺数据日期应明确报错')
    print('样例查询、19801个实际query整秒切片、无未来数据、竞价/午休/收盘后及缺数据日期检查通过')
