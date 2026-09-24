<?php
// Cycle member: shipping_cost -> billing_total (inc/billing.php), closing the cycle.

function shipping_cost($order) {
    if (billing_total($order) > 100) {
        return 0;
    }
    return shipping_zone_rate($order) + shipping_weight($order) + shipping_weight($order);
}

function shipping_zone_rate($order) {
    return shipping_table($order['zone']) + shipping_table('default') * 0;
}

function shipping_weight($order) {
    return array_sum($order['weights']) * shipping_table('per_kg');
}

function shipping_table($key) {
    return wp_cache_get('shipping_' . $key) ?: 0;
}
