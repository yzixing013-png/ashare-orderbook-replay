"""北京时间盘口还原：python market_slice.py "2024-02-07 09:30:05"。"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


def read_data(folder, name, time_column):
    """清洗关键字段；按业务序号去重，不能按订单号去重成交。"""
    df = pd.read_csv(Path(folder) / name)
    original_count = len(df)
    if time_column == "time":
        clock = pd.to_numeric(df.time, errors="coerce")
        h, m, s = clock // 10000000, clock // 100000 % 100, clock // 1000 % 100
        valid = clock.ge(0) & h.lt(24) & m.lt(60) & s.lt(60) & clock.mod(1).eq(0)
        day = pd.to_datetime(df.date.astype(str), format="%Y%m%d", errors="coerce")
        df["timestamp"] = (day + pd.to_timedelta(
            (h * 3600000 + m * 60000 + s * 1000 + clock % 1000).where(valid, 0).fillna(0), unit="ms"
        )).dt.tz_localize("Asia/Shanghai").where(valid)
        cols = ["last", "pre_close", "open", "high", "low", "volume", "amount",
                "num_trades", "total_bid_vol", "total_ask_vol"]
        cols += [f"{p}{kind}{i}" for p in "ab" for kind in "pv" for i in range(1, 11)]
        keys, sorting = ["symbol", "timestamp"], ["timestamp"]
    else:
        stamp = df[time_column].astype('string')
        has_zone = stamp.str.contains(r'(?:Z|[+-]\d{2}:?\d{2})$', na=False)
        stamp = stamp.where(has_zone, stamp + '+08:00')
        df["timestamp"] = pd.to_datetime(stamp, format="ISO8601",
                                          errors="coerce", utc=True).dt.tz_convert("Asia/Shanghai")
        cols = ["price", "quantity", "channel", "index"]
        keys = ["symbol", "date", "channel", "index"]
        sorting = ["timestamp", "channel", "index"]
    df[cols] = df[cols].apply(pd.to_numeric, errors="coerce")
    valid = np.isfinite(df[cols]).all(axis=1) & df[cols].ge(0).all(axis=1)
    if time_column != "time":
        valid &= df.price.gt(0) & df.quantity.gt(0) & df.quantity.mod(1).eq(0)
        valid &= df.channel.mod(1).eq(0) & df["index"].mod(1).eq(0)
        date = pd.to_datetime(df.date.astype(str), format="%Y%m%d", errors="coerce")
        valid &= df.timestamp.dt.date.eq(date.dt.date)
        if time_column == "order_time":
            valid &= df.side.isin(["buy", "sell"]) & df.order_type.isin(["place", "cancel"])
    else:
        integers = ["volume", "num_trades", "total_bid_vol", "total_ask_vol"]
        integers += [f"{p}v{i}" for p in "ab" for i in range(1, 11)]
        valid &= df[integers].mod(1).eq(0).all(axis=1)
    df = (df[valid].dropna(subset=["symbol", "timestamp"])
          .drop_duplicates(keys, keep="last").sort_values(sorting, kind="stable"))
    df = df.reset_index(drop=True)
    df.attrs["removed"] = original_count - len(df)
    return df


def local_time(value):
    value = pd.Timestamp(value)
    if pd.isna(value):
        raise ValueError("请输入有效的时间戳。")
    return value.tz_localize("Asia/Shanghai") if value.tzinfo is None else value.tz_convert("Asia/Shanghai")


class MarketReplay:
    """一次加载、重复查询；保留所有价位，避免第十一档补位丢失。"""
    def __init__(self, folder, date, symbol="SH.510050", amount_scale=10000):
        self.symbol, self.date, self.scale = symbol, local_time(date).date(), amount_scale
        if not np.isfinite(amount_scale) or amount_scale <= 0:
            raise ValueError("amount_scale 必须是正数。")
        self.cleaned, frames = {}, []
        for name, col in [("df_level2.csv", "time"), ("df_order.csv", "order_time"),
                          ("df_trade.csv", "trade_time")]:
            df = read_data(folder, name, col)
            self.cleaned[name] = df.attrs["removed"]
            frames.append(df[(df.symbol == symbol) & (df.timestamp.dt.date == self.date)].reset_index(drop=True))
        self.snapshots, self.orders, self.trades = frames
        if self.snapshots.empty or self.orders.empty:
            raise ValueError("该证券当日缺少快照或逐笔委托；样例日期为 2024-02-07。")
        # CSV 委托数量是进入盘口的挂单数量；主动成交单不能再次从盘口扣减。
        side = self.trades.other_info.astype(str).map({"external(buy)": "sell", "internal(sell)": "buy"})
        if side.isna().any():
            raise ValueError("成交内外盘标志缺失或未知，不能可靠判断被动挂单侧。")
        self.order_events = [(r.side, int(round(r.price * 10000)),
                              int(r.quantity) * (1 if r.order_type == "place" else -1))
                             for r in self.orders.itertuples()]
        self.trade_events = [(s, int(round(r.price * 10000)), -int(r.quantity))
                             for s, r in zip(side, self.trades.itertuples())]
        # 各价位的前缀和：重复查询不用每次回放几十万条历史记录。
        self.level_history = []
        for events in [self.order_events, self.trade_events]:
            levels = defaultdict(list)
            for i, (s, p, q) in enumerate(events):
                levels[s, p].append((i, q))
            self.level_history.append({key: (np.array([i for i, _ in values]),
                np.r_[0, np.cumsum([q for _, q in values])]) for key, values in levels.items()})
        self._boundary_cache = {}
        self.cum_volume = np.r_[0, self.trades.quantity.to_numpy(dtype="int64").cumsum()]
        self.cum_amount = np.r_[0., (self.trades.price * self.trades.quantity).cumsum()]
        self.passive = {s: np.r_[0, np.where(side == s, self.trades.quantity, 0).cumsum()]
                        for s in ["buy", "sell"]}
        self.order_cumulative = {s: np.r_[0, np.cumsum([q if x == s else 0
                                  for x, _, q in self.order_events])] for s in ["buy", "sell"]}
        self.order_lookup = defaultdict(list)
        for n, key in enumerate(zip(self.order_cumulative["buy"], self.order_cumulative["sell"])):
            self.order_lookup[key].append(n)
        # 样例成交从 09:30 起，缺少集合竞价成交：从开盘竞价后的快照补初始状态。
        first_trade = self.trades.timestamp.iloc[0] if len(self.trades) else self.snapshots.timestamp.iloc[-1]
        anchors = self.snapshots[(self.snapshots.timestamp < first_trade) & self.snapshots.volume.gt(0)]
        self.auction = anchors.iloc[0] if len(anchors) else None
        self.initial_volume = int(self.auction.volume) if self.auction is not None else 0
        self.initial_count = int(self.auction.num_trades) if self.auction is not None else 0
        self.initial_amount = float(self.auction.amount) * self.scale if self.auction is not None else 0.
        self.auction_changes = []
        if self.auction is not None:
            candidates = self.order_lookup.get((int(self.auction.total_bid_vol) + self.initial_volume,
                                                int(self.auction.total_ask_vol) + self.initial_volume), [])
            candidates = [n for n in candidates if n == 0 or self.orders.timestamp.iloc[n-1] <= self.auction.timestamp]
            if not candidates:
                raise ValueError("无法对齐集合竞价初始状态；委托记录可能缺失。")
            book = self.book_at(candidates[-1], 0, include_auction=False)
            auction_price = int(round(self.auction['last'] * 10000))
            for s in book:
                left = self.initial_volume
                for p in sorted(book[s], reverse=s == "buy"):
                    if (s == "buy" and p < auction_price) or (s == "sell" and p > auction_price):
                        break
                    q = min(left, max(book[s][p], 0))
                    if q:
                        self.auction_changes.append((s, p, -q))
                    left -= q
                    if not left:
                        break
                if left:
                    raise ValueError("集合竞价委托量不足，不能还原初始盘口。")

    @staticmethod
    def apply(book, events):
        # 两路消息独立计序，处理中截零会永久丢失扣减量；仅在最终结果检查负余额。
        for side, price, qty in events:
            book[side][price] += qty

    def book_at(self, order_count, trade_count, include_auction=True):
        book = {"buy": defaultdict(int), "sell": defaultdict(int)}
        for count, levels in zip([order_count, trade_count], self.level_history):
            for (side, price), (indices, quantities) in levels.items():
                book[side][price] += int(quantities[np.searchsorted(indices, count)])
        if include_auction:
            self.apply(book, self.auction_changes)
        return book

    def snapshot_book(self, snap, ntrade, include_auction):
        """用快照总量定位消息边界，并实际核对/校正十档；不确定时拒绝猜测。"""
        cache_key = (snap.timestamp, ntrade, include_auction)
        if cache_key in self._boundary_cache:
            return self._boundary_cache[cache_key]
        if len(self._boundary_cache) >= 32:
            self._boundary_cache.clear()
        initial = self.initial_volume if include_auction else 0
        key = (int(snap.total_bid_vol) + int(self.passive['buy'][ntrade]) + initial,
               int(snap.total_ask_vol) + int(self.passive['sell'][ntrade]) + initial)
        # 快照时间是整秒标签，数据可能覆盖该秒内更晚的事件。
        limit = int(self.orders.timestamp.searchsorted(snap.timestamp + pd.Timedelta(seconds=1), side='left'))
        candidates = [n for n in self.order_lookup.get(key, []) if n <= limit]
        if not candidates:
            raise ValueError(f"快照 {snap.timestamp} 的总委托量无法对齐：可能缺失委托，不能可靠还原盘口。")
        expected = {s: [{"price": float(snap[f'{p}p{i}']), "quantity": int(snap[f'{p}v{i}'])}
                       for i in range(1, 11)] for s, p in [('buy', 'b'), ('sell', 'a')]}
        for norder in candidates:
            book = self.book_at(norder, ntrade, include_auction)
            if all(self.top10(book, s) == expected[s] for s in book):
                result = (norder, book, False)
                self._boundary_cache[cache_key] = result
                return result
        # 只有一个候选边界时才能按快照修正；多个候选不能用最小误差猜一个。
        if len(candidates) != 1:
            raise ValueError(f"快照 {snap.timestamp} 的委托边界有歧义，不能可靠修正盘口。")
        for s in book:
            quotes = {int(round(x['price'] * 10000)): x['quantity'] for x in expected[s] if x['price'] > 0}
            edge = (min(quotes) if s == 'buy' else max(quotes)) if quotes else None
            for p in list(book[s]):
                if edge is None or (s == 'buy' and p >= edge) or (s == 'sell' and p <= edge):
                    book[s][p] = 0
            book[s].update(quotes)
        if (sum(book['buy'].values()) != int(snap.total_bid_vol)
                or sum(book['sell'].values()) != int(snap.total_ask_vol)
                or any(q < 0 for levels in book.values() for q in levels.values())):
            raise ValueError("快照十档外仍有数量缺口，不能精确恢复深档盘口。")
        result = (candidates[0], book, True)
        self._boundary_cache[cache_key] = result
        return result

    @staticmethod
    def top10(book, side):
        levels = sorted(((p, q) for p, q in book[side].items() if q > 0), reverse=side == "buy")[:10]
        return ([{"price": p / 10000, "quantity": int(q)} for p, q in levels]
                + [{"price": 0., "quantity": 0} for _ in range(10 - len(levels))])

    def query(self, target):
        target = local_time(target)
        if target.date() != self.date:
            raise ValueError("查询日期与加载日期不同。")
        no = int(self.orders.timestamp.searchsorted(target, side="right"))
        nt = int(self.trades.timestamp.searchsorted(target, side="right"))
        auction_done = self.auction is not None and self.auction.timestamp <= target
        # 快照整秒被截断，可能已包含目标之后的成交：按成交笔数排除这些快照。
        stop = int(self.snapshots.timestamp.searchsorted(target, side='right'))
        offset = self.initial_count if auction_done else 0
        counts = self.snapshots.num_trades.to_numpy()[:stop] - offset
        eligible = np.flatnonzero((counts >= 0) & (counts <= nt))
        if not len(eligible):
            raise ValueError("目标时刻之前没有可用的参考快照。")
        # 验证完整快照已包含的委托也不晚于 target，避免只排除未来成交而漏掉未来委托。
        aligned = False
        corrected = False
        for index in eligible[::-1]:
            snap = self.snapshots.iloc[index].copy()
            snap['trade_count'] = int(counts[index])
            if auction_done or nt > 0:
                try:
                    norder, baseline, corrected = self.snapshot_book(snap, int(snap.trade_count), auction_done)
                except ValueError:
                    if target < snap.timestamp + pd.Timedelta(seconds=1):
                        continue  # 该秒内的快照事件可能尚未完整到达，退到更早的快照。
                    raise
                if norder > no:
                    continue
                aligned = True
            break
        else:
            raise ValueError("目标时刻之前没有已完整到达的盘口快照。")
        nbase = int(snap.trade_count)
        volume = (self.initial_volume if auction_done else 0) + int(self.cum_volume[nbase])
        amount = (self.initial_amount if auction_done else 0) + float(self.cum_amount[nbase])
        # 样例快照金额保留四位万元（约一元精度），允许相应的四舍五入差。
        if int(snap.volume) != volume or abs(float(snap.amount) * self.scale - amount) > self.scale * 0.00005 + 0.01:
            raise ValueError("快照与逐笔成交累计量/额不一致，请检查数据完整性或金额倍率。")
        if aligned:
            # 快照校正后的完整价格簿作为基础，之后只回放尚未包含的事件。
            book = {s: defaultdict(int, levels) for s, levels in baseline.items()}
            self.apply(book, self.order_events[norder:no])
            self.apply(book, self.trade_events[nbase:nt])
        else:
            book = self.book_at(no, nt, include_auction=auction_done)
        warnings = []
        if any(q < 0 for levels in book.values() for q in levels.values()):
            raise ValueError("盘口存在负余额：快照后的逐笔记录缺失或不同步，不能可靠还原。")
        prices = self.trades.price.iloc[nbase:nt]
        last, opening, high, low = map(float, [snap['last'], snap.open, snap.high, snap.low])
        if len(prices):
            last = float(prices.iloc[-1])
            opening = opening or float(prices.iloc[0])
            high = max(high, float(prices.max()))
            low = min(low, float(prices.min())) if low else float(prices.min())
        bids, asks = self.top10(book, "buy"), self.top10(book, "sell")
        phase = "continuous" if nt else ("after_auction" if auction_done else "auction")
        if "E" in str(snap.get('trading_phase_code', '')):
            phase = "closed"
        if phase == "continuous" and bids[0]['price'] and asks[0]['price'] and bids[0]['price'] >= asks[0]['price']:
            raise ValueError("连续交易盘口交叉：两路逐笔记录可能不同步，不能可靠还原。")
        return {"symbol": self.symbol, "timestamp": target.isoformat(),
                "snapshot_time": snap.timestamp.isoformat(), "last": last,
                "pre_close": float(snap.pre_close), "open": opening, "high": high, "low": low,
                "volume": int(snap.volume) + int(self.cum_volume[nt] - self.cum_volume[nbase]),
                "amount": round((self.initial_amount if auction_done else 0.) + float(self.cum_amount[nt]), 4),
                "amount_unit": "元", "num_trades": int(snap.num_trades) + nt - nbase,
                "total_bid_vol": int(sum(book['buy'].values())), "total_ask_vol": int(sum(book['sell'].values())),
                "bids": bids, "asks": asks, "phase": phase,
                "trading_phase_code": str(snap.get('trading_phase_code', '')),
                "warnings": warnings,
                "snapshot_corrected": corrected,
                "cleaned_rows": self.cleaned}


def market_slice(target, folder=None, symbol="SH.510050", amount_scale=10000):
    return MarketReplay(folder or Path(__file__).parent, target, symbol, amount_scale).query(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="指定时刻的盘口行情（北京时间）")
    parser.add_argument("timestamp", help="例如 2024-02-07 09:30:05")
    parser.add_argument("--folder", default=str(Path(__file__).parent))
    parser.add_argument("--symbol", default="SH.510050")
    parser.add_argument("--amount-scale", type=float, default=10000, help="快照金额转为元的倍率")
    args = parser.parse_args()
    try:
        print(json.dumps(market_slice(args.timestamp, args.folder, args.symbol, args.amount_scale),
                         ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, KeyError, FileNotFoundError) as exc:
        parser.exit(1, f"输入或数据错误：{exc}\n")
