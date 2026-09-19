Tutorial dataset: 50 short support tickets with the queue each should be routed to
(billing, shipping, refund, bug, account, other). Routing rules the tutorial's root prompt does not know:
a request for money back is `refund` whatever caused it; double charges and wrong amounts are `billing`;
subscription changes, login and deletion are `account`; things that do not work are `bug`; praise, feature
requests and general questions are `other`. ~30% of rows are deliberately ambiguous between two queues.
