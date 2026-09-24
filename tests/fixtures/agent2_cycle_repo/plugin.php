<?php
/*
Plugin Name: Agent 2 cycle fixture
*/

require_once __DIR__ . '/inc/billing.php';
require_once __DIR__ . '/inc/tax.php';
require_once __DIR__ . '/inc/shipping.php';

function cycle_register() {
    register_post_type('invoice', ['public' => false]);
}
add_action('init', 'cycle_register');

function cycle_render_invoice($post_id) {
    $order = get_post_meta($post_id, '_order', true);
    update_post_meta($post_id, '_total', billing_format($order));
}
add_action('save_post', 'cycle_render_invoice');
