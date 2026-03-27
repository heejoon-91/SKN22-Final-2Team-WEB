#!/usr/bin/env bash

# Dump / restore 대상인 앱 데이터 테이블 목록.
# 스키마는 Django migrations가 생성하고, 이 목록의 데이터만 별도로 백업/복원한다.
APP_DATA_TABLES=(
    user
    user_profile
    social_account
    user_preference
    user_used_product
    pet
    pet_health_concern
    pet_allergy
    pet_food_preference
    pet_used_product
    cart
    cart_item
    wishlist
    wishlist_item
    order
    order_item
    user_interaction
    chat_session
    chat_message
    product
    product_category_tag
    review
    product_admin_config
    domain_qna
    breed_meta
    social_auth_usersocialauth
    social_auth_nonce
    social_auth_association
    social_auth_code
    social_auth_partial
)
