"""Agent tools package - exports all tools."""
# Import common utilities first
from app.agent.tools.common import set_current_user_phone, _get_current_user_phone

# Import all tools from each module
from app.agent.tools.product_tools import (
    search_product,
    show_more_products,
    select_product_from_last_search,
    select_last_product_from_last_search,
    filter_last_search_by_budget,
    find_upsell_product,
    find_cross_sell_product,
    get_product_details,
)

from app.agent.tools.cart_tools import (
    add_to_cart,
    add_catalog_product_to_cart,
    remove_from_cart,
    remove_from_cart_by_product_id,
    retain_only_item_in_cart,
    clear_cart,
    summarize_cart,
    show_cart_items,
    check_cart_inventory_and_cleanup,
    retain_cart_items_by_position,
    calculate_price_difference,
)

from app.agent.tools.checkout_tools import (
    get_or_set_delivery_address,
    proceed_to_delivery_setup,
    process_address_response,
    calculate_delivery_options,
    get_delivery_quote,
    delivery_zones_info,
    compare_delivery_options,
    create_order_from_cart,
    checkout_with_selected_items,
    calculate_checkout_total_with_delivery,
    process_final_checkout_with_mpesa,
    check_payment_status,
    buy_product,
    generate_and_send_receipt,
)

from app.agent.tools.order_tools import (
    get_user_orders,
    get_user_pending_orders_fresh,
    cancel_order,
    get_order_details,
)

from app.agent.tools.image_tools import (
    analyze_standalone_image,
)

from app.agent.tools.trade_in_tools import (
    search_product_price_kenya,
    calculate_trade_in_valuation,
    create_trade_in_session,
    cancel_active_session,
    update_session_with_product_info,
    add_images_to_trade_in_session,
    get_trade_in_session_data,
    get_pending_trade_in_sessions,
    complete_trade_in_assessment,
    accept_trade_in_offer,
    reject_trade_in_offer,
    negotiate_trade_in_counter,
    set_redemption_method,
    get_pending_trade_ins,
    mark_trade_in_as_completed,
    get_store_credit_balance,
    use_store_credit,
    generate_category_specific_questions,
    store_condition_response,
)

from app.agent.tools.user_tools import (
    get_user_info,
    get_store_info,
)

# Export ALL_TOOLS list
ALL_TOOLS = [
    # Product Discovery & Search
    search_product,
    show_more_products,
    select_product_from_last_search,
    get_product_details,
    
    # Cart Management
    add_to_cart,
    add_catalog_product_to_cart,
    remove_from_cart,
    remove_from_cart_by_product_id,
    retain_only_item_in_cart,
    clear_cart,
    summarize_cart,
    show_cart_items,
    check_cart_inventory_and_cleanup,
    retain_cart_items_by_position,
    calculate_checkout_total_with_delivery,
    
    # Order creation & details
    create_order_from_cart,
    checkout_with_selected_items,
    get_order_details,
    
    # Product Upselling
    find_upsell_product,
    find_cross_sell_product,
    calculate_price_difference,
    
    # Search result selection helpers
    select_product_from_last_search,
    select_last_product_from_last_search,
    filter_last_search_by_budget,
    
    # Checkout & Delivery
    get_or_set_delivery_address,
    proceed_to_delivery_setup,
    process_address_response,
    calculate_delivery_options,
    get_delivery_quote,
    delivery_zones_info,
    compare_delivery_options,
    
    # Payment Processing
    process_final_checkout_with_mpesa,
    check_payment_status,
    buy_product,
    generate_and_send_receipt,
    
    # Order Management
    get_user_orders,
    get_user_pending_orders_fresh,
    cancel_order,
    
    # Image Processing
    analyze_standalone_image,
    
    # Trade-in & Valuation
    search_product_price_kenya,
    calculate_trade_in_valuation,
    create_trade_in_session,
    cancel_active_session,
    update_session_with_product_info,
    add_images_to_trade_in_session,
    get_trade_in_session_data,
    get_pending_trade_in_sessions,
    complete_trade_in_assessment,
    accept_trade_in_offer,
    reject_trade_in_offer,
    negotiate_trade_in_counter,
    set_redemption_method,
    get_pending_trade_ins,
    mark_trade_in_as_completed,
    get_store_credit_balance,
    use_store_credit,
    
    # Category-specific questions for AI pricing
    generate_category_specific_questions,
    store_condition_response,
    
    # General Support
    get_user_info,
    get_store_info,
]
