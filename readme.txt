测试：python3 -m unittest discover -s tests/journey_attribution -p 'test_*.py'
构建：python3 -m compileall -q twin_market_flow tests/journey_attribution
验收：python3 -m twin_market_flow.api.command fixtures/demo.json --database /tmp/twin_market_flow-acceptance.sqlite3
所有输入均为本地合成数据，运行不需要网络。
