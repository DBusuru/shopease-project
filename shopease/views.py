from django.db import migrations, models
from django.forms import ValidationError
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.urls import reverse
import json
from django.core.validators import validate_email

from shopease.models import Newsletter
from .models import CartItem, Product, Category, Brand, ProductVariant, Review, Order, OrderItem, Wishlist, WishlistItem, NewsletterSubscriber, InstallmentPlan, InstallmentPayment, Contact
from django.db.models import Avg
from decimal import Decimal
from django.db.models import Q
from django.db.models import Min, Max
from django.views.decorators.csrf import csrf_protect
from shopping_cart.models import CartItem, Cart
import logging
import requests
import random
from django.conf import settings
from django.utils import timezone
from .mpesa_utils import MpesaClient
from .models import MpesaPayment
from django.views.decorators.csrf import csrf_exempt
from django.core.mail import send_mail
from django.db.models import Count, F
from datetime import datetime, timedelta
from .models import BNPLInstallment
import base64
from users.models import InstallmentPlan, InstallmentPayment
import string

from shopease import mpesa_utils

logger = logging.getLogger(__name__)

class Migration(migrations.Migration):
    dependencies = [
        ('shopease', 'previous_migration'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='discount',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
    ]

def index(request):
    # Get new products (ordered by creation date)
    new_products = Product.objects.select_related('category').all().order_by('-created_at')[:12]
    
    # Get top selling products (based on order count)
    top_selling = Product.objects.select_related('category').annotate(
        sold_count=Count('orderitem')
    ).order_by('-sold_count')[:12]
    
    context = {
        'new_products': new_products,
        'top_selling': top_selling,  # Add top selling products to context
    }
    return render(request, 'index.html', context)

def category(request, category_slug):
    categories = Category.objects.all()
    category = Category.objects.get(slug=category_slug)
    products = Product.objects.filter(category=category)
    
    context = {
        'categories': categories,
        'products': products,
        'active_category': category.name
    }
    return render(request, 'index.html', context)

@login_required
def checkout(request):
    # Get cart for the current user
    cart, _ = Cart.objects.get_or_create(user=request.user)
    
    # Get cart items using the cart relationship
    cart_items = CartItem.objects.filter(cart=cart).select_related('product')
    
    # Calculate total
    total = sum(item.quantity * item.product.price for item in cart_items)
    
    # Handle payment type selection
    payment_type = request.GET.get('payment_type')
    
    if payment_type == 'full':
        # Store total amount in session for payment status
        request.session['amount'] = str(total)
        request.session['is_installment'] = False
        return redirect('shopease:payment_status_full')
    elif payment_type == 'installment':
        # Calculate monthly installment
        monthly_installment = total / 6
        request.session['is_installment'] = True
        request.session['monthly_payment'] = str(monthly_installment)
        request.session['total_amount'] = str(total)
        request.session['total_installments'] = 6
        request.session['installment_number'] = 1
        request.session['amount'] = str(monthly_installment)
        return redirect('shopease:select_installment_plan')
    
    # Calculate monthly installment for display
    monthly_installment = total / 6
    
    context = {
        'cart_items': cart_items,
        'total': total,
        'monthly_installment': monthly_installment,
    }
    
    return render(request, 'checkout.html', context)

@login_required
def order_confirmation(request):
    # Get order details from session
    order_details = {
        'total_amount': request.session.get('total_amount'),
        'months': request.session.get('installment_months'),
        'monthly_payment': request.session.get('monthly_payment'),
    }
    
    # Clear the session data
    request.session.pop('total_amount', None)
    request.session.pop('installment_months', None)
    request.session.pop('monthly_payment', None)
    
    return render(request, 'order_confirmation.html', {'order_details': order_details})

def product_list(request):
    # Get all products or your filtered queryset
    products_list = Product.objects.all()
    
    # Create a paginator object
    paginator = Paginator(products_list, 12)  # Show 12 products per page
    
    # Get the page number from the request
    page = request.GET.get('page', 1)
    
    # Get the products for the current page
    products = paginator.get_page(page)
    
    context = {
        'products': products,
    }
    return render(request, 'product_list.html', context)

@login_required
def delete_product(request, product_id):
    # Only allow staff/admin to delete products
    if not request.user.is_staff:
        messages.error(request, "You don't have permission to delete products.")
        return redirect('shopease:index')
    
    # Get the product or return 404
    product = get_object_or_404(Product, id=product_id)
    
    try:
        # Delete the product
        product.delete()
        messages.success(request, f'Product "{product.name}" has been deleted successfully.')
    except Exception as e:
        messages.error(request, f'Error deleting product: {str(e)}')
    
    # Redirect back to product list or admin dashboard
    return redirect('product_list')

@login_required
def view_cart(request):
    # Get or create the user's cart
    cart, created = Cart.objects.get_or_create(user=request.user)
    
    # Get cart items with related product data
    cart_items = CartItem.objects.filter(cart=cart).select_related('product')
    
    # Calculate total
    total = sum(item.quantity * item.product.price for item in cart_items)
    
    context = {
        'cart_items': cart_items,
        'total': total,
    }
    
    print("Cart items:", list(cart_items))  # Debug print
    return render(request, 'cart.html', context)

@login_required
@require_POST
def update_cart_quantity(request, item_id):
    try:
        cart_item = CartItem.objects.get(id=item_id, user=request.user)
        data = json.loads(request.body)
        quantity = int(data.get('quantity', 1))
        
        if quantity > 0:
            cart_item.quantity = quantity
            cart_item.save()
            return JsonResponse({'success': True})
        else:
            return JsonResponse({'success': False, 'error': 'Invalid quantity'})
    except CartItem.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Item not found'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@login_required
@require_POST
def remove_from_cart(request, item_id):
    try:
        cart_item = get_object_or_404(CartItem, id=item_id, cart__user=request.user)
        cart = cart_item.cart
        cart_item.delete()
        
        # Calculate new cart total using CartItem.objects.filter instead of cart.items.all()
        total = sum(item.product.price * item.quantity for item in CartItem.objects.filter(cart=cart))
        
        return JsonResponse({
            'success': True,
            'cart_total': float(total)
        })
    except CartItem.DoesNotExist:
        return JsonResponse({
            'success': False,
            'message': 'Item not found'
        }, status=404)

@login_required
@require_POST
def add_to_cart(request, product_id):
    if request.method == 'POST':
        try:
            product = Product.objects.get(id=product_id)
            cart, created = Cart.objects.get_or_create(user=request.user)
            cart_item, item_created = CartItem.objects.get_or_create(cart=cart, product=product)
            
            if not item_created:
                cart_item.quantity += 1
                cart_item.save()
            
            messages.success(request, f"{product.name} added to cart successfully!")
            return redirect(request.META.get('HTTP_REFERER', 'shopease:product_list'))
            
        except Product.DoesNotExist:
            messages.error(request, "Product not found.")
            return redirect('shopping_cart:cart_detail')
        except Exception as e:
            messages.error(request, f"Error adding product to cart: {str(e)}")
    
    return redirect('shopease:product_list')

@login_required
def wishlist(request):
    wishlist, created = Wishlist.objects.get_or_create(user=request.user)
    context = {
        'wishlist_items': wishlist.products.all()
    }
    return render(request, 'wishlist.html', context)

@login_required
def add_to_wishlist(request, product_id):
    if request.method == 'POST':
        product = get_object_or_404(Product, id=product_id)
        wishlist, created = Wishlist.objects.get_or_create(user=request.user)
        
        if wishlist.products.filter(id=product_id).exists():
            messages.warning(request, 'Product is already in your wishlist.')
        else:
            wishlist.products.add(product)
            messages.success(request, f"{product.name} has been added to your wishlist.")
    
    # Redirect back to the previous page
    return redirect(request.META.get('HTTP_REFERER', 'shopease:product_list'))

@login_required
def remove_from_wishlist(request, item_id):
    wishlist_item = get_object_or_404(WishlistItem, id=item_id, user=request.user)
    wishlist_item.delete()
    messages.success(request, 'Item removed from wishlist!')
    return redirect('shopease:wishlist')

@login_required
def toggle_wishlist(request, product_id):
    if request.method == 'POST':
        product = get_object_or_404(Product, id=product_id)
        wishlist, created = Wishlist.objects.get_or_create(user=request.user)
        
        if product in wishlist.products.all():
            wishlist.products.remove(product)
            messages.success(request, f"{product.name} removed from your wishlist.")
        else:
            wishlist.products.add(product)
            messages.success(request, f"{product.name} added to your wishlist.")
            
    return redirect('shopease:product_detail', product_id=product_id)

def product_detail(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    context = {
        'product': product,
    }
    return render(request, 'product_details.html', context)

@login_required
def process_checkout(request):
    if request.method == 'POST':
        # Get cart items
        cart_items = CartItem.objects.filter(user=request.user)
        
        if not cart_items.exists():
            messages.error(request, 'Your cart is empty!')
            return redirect('shopease:view_cart')
        
        try:
            # Create order
            order = Order.objects.create(
                user=request.user,
                first_name=request.POST.get('first_name'),
                last_name=request.POST.get('last_name'),
                email=request.POST.get('email'),
                address=request.POST.get('address'),
                city=request.POST.get('city'),
                country=request.POST.get('country'),
                postal_code=request.POST.get('zip_code'),
                phone=request.POST.get('phone'),
                payment_method=request.POST.get('payment_method', 'Credit Card'),
                total_amount=sum(item.get_total() for item in cart_items)
            )
            
            # Create order items
            for cart_item in cart_items:
                OrderItem.objects.create(
                    order=order,
                    product=cart_item.variant.product,
                    variant=cart_item.variant,
                    quantity=cart_item.quantity,
                    price=cart_item.variant.price
                )
            
            # Clear cart
            cart_items.delete()
            
            # Send order confirmation email (implement this later)
            # send_order_confirmation_email(order)
            
            messages.success(request, 'Order placed successfully!')
            return redirect('shopease:thank_you_with_order', order_id=order.id)
            
        except Exception as e:
            messages.error(request, f'Error processing order: {str(e)}')
            return redirect('shopease:checkout')
    
    return redirect('shopease:checkout')

@login_required
def thank_you(request):
    try:
        # Get the latest order for this user
        order = Order.objects.filter(user=request.user).latest('created_at')
        
        # Clear the cart items first
        CartItem.objects.filter(cart__user=request.user).delete()
        
        # Then delete the cart itself
        Cart.objects.filter(user=request.user).delete()
        
        messages.success(request, 'Thank you for your order! Your cart has been cleared.')
        
        context = {
            'order': order
        }
        
    except Order.DoesNotExist:
        messages.warning(request, 'No order found.')
        context = {}
    except Exception as e:
        logger.error(f"Error in thank_you view: {str(e)}")
        messages.error(request, 'There was an error processing your order.')
        context = {}
    
    return render(request, 'thank_you.html', context)

def search_products(request):
    query = request.GET.get('q', '')
    category_id = request.GET.get('category', '')
    
    products = Product.objects.all()
    
    if query:
        products = products.filter(name__icontains=query)
    
    if category_id:
        products = products.filter(category_id=category_id)
    
    context = {
        'products': products,
        'search_query': query,
        'selected_category': category_id
    }
    
    return render(request, 'search_results.html', context)

def smartphones(request):
    try:
        # Get main Smartphones category and its subcategories
        smartphones_category = Category.objects.get(name='Smartphones')
        subcategories = Category.objects.filter(parent=smartphones_category)
        
        # Get selected subcategory from URL parameter
        selected_subcategory = request.GET.get('subcategory')
        
        # Get all smartphones
        products = Product.objects.filter(
            category__in=[smartphones_category] + list(subcategories)
        ).select_related('category')
        
        # Filter by subcategory if selected
        if selected_subcategory:
            products = products.filter(category__slug=selected_subcategory)
        
        # Get price range
        min_price = request.GET.get('min_price')
        max_price = request.GET.get('max_price')
        
        # Apply price filter if provided
        if min_price:
            products = products.filter(price__gte=min_price)
        if max_price:
            products = products.filter(price__lte=max_price)
            
        # Apply sorting
        sort = request.GET.get('sort')
        if sort == 'price_low':
            products = products.order_by('price')
        elif sort == 'price_high':
            products = products.order_by('-price')
        elif sort == 'newest':
            products = products.order_by('-created_at')
        elif sort == 'name_asc':
            products = products.order_by('name')
            
        # Pagination - show 10 products per page
        paginator = Paginator(products, 10)  # Changed from 12 to 10
        page = request.GET.get('page')
        try:
            products = paginator.get_page(page)
        except PageNotAnInteger:
            products = paginator.get_page(1)
        except EmptyPage:
            products = paginator.get_page(paginator.num_pages)
        
        context = {
            'products': products,
            'min_price': min_price,
            'max_price': max_price,
            'selected_sort': sort,
            'subcategories': subcategories,
            'selected_subcategory': selected_subcategory,
            'main_category': smartphones_category,
            'total_products': paginator.count,
            'current_page': products.number,
            'total_pages': paginator.num_pages
        }
        return render(request, 'smartphones.html', context)
        
    except Category.DoesNotExist:
        messages.error(request, 'Smartphones category not found')
        return redirect('shopease:index')

def hot_deals(request):
    # Get products with discounts
    products = Product.objects.filter(discount__gt=0).order_by('-discount')
    print(f"Found {products.count()} products with discounts")  # Debug print
    
    for product in products:  # Debug print
        print(f"Product: {product.name}, Discount: {product.discount}%")
    
    # Get filter parameters
    min_price = request.GET.get('min_price')
    max_price = request.GET.get('max_price')
    selected_sort = request.GET.get('sort')
    
    # Apply filters
    if min_price:
        products = products.filter(price__gte=min_price)
    if max_price:
        products = products.filter(price__lte=max_price)
    
    # Apply sorting
    if selected_sort:
        if selected_sort == 'price_low':
            products = products.order_by('price')
        elif selected_sort == 'price_high':
            products = products.order_by('-price')
        elif selected_sort == 'newest':
            products = products.order_by('-created_at')
        elif selected_sort == 'name_asc':
            products = products.order_by('name')
    
    # Pagination
    paginator = Paginator(products, 9)  # 9 products per page
    page = request.GET.get('page')
    products = paginator.get_page(page)
    
    context = {
        'products': products,
        'min_price': min_price,
        'max_price': max_price,
        'selected_sort': selected_sort,
    }
    return render(request, 'hot_deals.html', context)

def categories(request):
    # Get all categories and debug print
    categories = Category.objects.all()
    print("Available categories:", [{'id': c.id, 'name': c.name, 'slug': c.slug} for c in categories])
    
    # Get selected category
    selected_category_id = request.GET.get('category')
    selected_category = None
    products = []
    
    if selected_category_id:
        try:
            selected_category = Category.objects.get(id=selected_category_id)
            products = Product.objects.filter(category=selected_category)
            
            # Apply sorting if requested
            sort = request.GET.get('sort')
            if sort == 'price_low':
                products = products.order_by('price')
            elif sort == 'price_high':
                products = products.order_by('-price')
            elif sort == 'newest':
                products = products.order_by('-created_at')
            elif sort == 'name_asc':
                products = products.order_by('name')
            
            # Pagination
            paginator = Paginator(products, 9)  # Show 9 products per page
            page = request.GET.get('page')
            products = paginator.get_page(page)
            
        except Category.DoesNotExist:
            messages.error(request, "Category not found")
    
    context = {
        'categories': categories,
        'selected_category': selected_category,
        'products': products,
        'selected_sort': request.GET.get('sort'),
    }
    
    return render(request, 'categories.html', context)

def laptops(request):
    # Get all categories for reference
    all_categories = Category.objects.all()
    
    # Get laptop products (filter by category name 'Laptops')
    laptop_products = Product.objects.filter(category__name='Laptops').select_related('category')
    
    context = {
        'products': laptop_products,
        'categories': all_categories,
        'active_category': 'Laptops'
    }
    return render(request, 'laptops.html', context)

def accessories(request):
    # Get all accessories
    products = Product.objects.filter(category__name='Accessories')
    
    # Get unique subcategories for accessories
    accessory_types = products.values_list('subcategory', flat=True).distinct()
    
    # Get filter parameters
    min_price = request.GET.get('min_price')
    max_price = request.GET.get('max_price')
    selected_type = request.GET.get('type')
    selected_sort = request.GET.get('sort')
    
    # Apply filters
    if min_price:
        products = products.filter(price__gte=min_price)
    if max_price:
        products = products.filter(price__lte=max_price)
    if selected_type:
        products = products.filter(subcategory=selected_type)
        
    # Apply sorting
    if selected_sort:
        if selected_sort == 'price_low':
            products = products.order_by('price')
        elif selected_sort == 'price_high':
            products = products.order_by('-price')
        elif selected_sort == 'newest':
            products = products.order_by('-created_at')
        elif selected_sort == 'name_asc':
            products = products.order_by('name')
    
    # Pagination
    paginator = Paginator(products, 9)  # 9 products per page
    page = request.GET.get('page')
    products = paginator.get_page(page)
    
    context = {
        'products': products,
        'accessory_types': accessory_types,
        'min_price': min_price,
        'max_price': max_price,
        'selected_type': selected_type,
        'selected_sort': selected_sort,
    }
    return render(request, 'accessories.html', context)

@require_POST
@csrf_protect
def newsletter_signup(request):
    try:
        data = json.loads(request.body)
        email = data.get('email')
        
        if not email:
            return JsonResponse({'success': False, 'error': 'Email is required'})
        
        # Validate email format
        try:
            validate_email(email)
        except ValidationError:
            return JsonResponse({'success': False, 'error': 'Invalid email format'})
        
        # Check if email already exists
        if Newsletter.objects.filter(email=email).exists():
            return JsonResponse({'success': False, 'error': 'Email already subscribed'})
        
        # Save new subscription
        Newsletter.objects.create(email=email)
        
        return JsonResponse({
            'success': True,
            'message': 'Successfully subscribed to newsletter'
        })
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid request format'
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        })

def category_view(request, category_id):
    category = get_object_or_404(Category, id=category_id)
    products = Product.objects.filter(category=category)
    
    context = {
        'category': category,
        'products': products,
    }
    
    return render(request, 'shopease/category.html', context)

@login_required
def add_review(request, product_id):
    if request.method == 'POST':
        product = get_object_or_404(Product, id=product_id)
        rating = request.POST.get('rating')
        comment = request.POST.get('comment')
        
        # Create the review
        Review.objects.create(
            user=request.user,
            product=product,
            rating=rating,
            comment=comment
        )
        
        return redirect('shopease:product_detail', product_id=product_id)
    
    return redirect('shopease:product_detail', product_id=product_id)

@login_required
@require_POST
def initiate_mpesa(request):
    if request.method == 'POST':
        phone_number = request.POST.get('phone_number')
        if not phone_number:
            messages.error(request, 'Phone number is required')
            return redirect('shopease:checkout')
            
        # Format phone number if needed
        if phone_number.startswith('0'):
            phone_number = '254' + phone_number[1:]
        elif not phone_number.startswith('254'):
            phone_number = '254' + phone_number
            
        # Get cart total
        cart = Cart.objects.get(user=request.user)
        cart_items = CartItem.objects.filter(cart=cart)
        total_amount = sum(item.quantity * item.product.price for item in cart_items)
        
        # Initialize M-PESA client
        mpesa_client = MpesaClient()
        
        # Generate reference number
        reference = f"ORDER-{request.user.id}-{timezone.now().strftime('%Y%m%d%H%M%S')}"
        
        # Initiate STK push
        response = mpesa_client.stk_push(
            phone_number=phone_number,
            amount=int(total_amount),
            reference=reference
        )
        
        if response.get('success'):
            # Store payment details in session
            request.session['amount'] = str(total_amount)
            request.session['mpesa_transaction_id'] = response.get('checkout_request_id')
            request.session['is_installment'] = False
            
            # Redirect to payment status page
            return redirect('shopease:payment_status_full')
        else:
            messages.error(request, response.get('message', 'Failed to initiate payment'))
            return redirect('shopease:checkout')
            
    return redirect('shopease:checkout')

@login_required
def payment_status_full(request):
    amount = request.session.get('amount')
    transaction_id = request.session.get('mpesa_transaction_id')
    
    if not amount:
        messages.error(request, 'Invalid payment amount')
        return redirect('shopease:checkout')
        
    context = {
        'amount': float(amount),
        'transaction_id': transaction_id
    }
    
    return render(request, 'payment_status_full.html', context)

@login_required
def check_payment_status(request, checkout_request_id):
    try:
        logger.info(f"Checking payment status for: {checkout_request_id}")
        
        # Get the payment record
        payment = MpesaPayment.objects.get(transaction_id=checkout_request_id)
        
        # Query M-PESA for status
        mpesa_client = MpesaClient()
        result = mpesa_client.query_payment_status(checkout_request_id)
        logger.info(f"Payment status result: {result}")
        
        if result.get('success'):
            # Update payment status
            payment.status = 'completed'
            payment.save()
            
            # Create order
            order = Order.objects.create(
                user=request.user,
                total_amount=payment.amount,
                payment_method='M-PESA',
                payment_status='completed',
                mpesa_payment=payment
            )
            
            # Move cart items to order items
            cart = Cart.objects.get(user=request.user)
            cart_items = CartItem.objects.filter(cart=cart)
            
            for cart_item in cart_items:
                OrderItem.objects.create(
                    order=order,
                    product=cart_item.product,
                    quantity=cart_item.quantity,
                    price=cart_item.product.price
                )
            
            # Clear the cart
            cart_items.delete()
            
            messages.success(request, 'Payment completed successfully!')
            
            # Check if this is a full payment or installment
            if not request.session.get('is_installment'):
                return JsonResponse({
                    'status': 'completed',
                    'redirect_url': reverse('shopease:thank_you')
                })
            else:
                return JsonResponse({
                    'status': 'completed',
                    'redirect_url': reverse('shopease:order_confirmation', kwargs={'order_id': order.id})
                })
        else:
            # Update payment status if failed
            payment.status = 'failed'
            payment.save()
            
            return JsonResponse({
                'status': 'error',
                'message': 'Payment failed. Please try again.'
            })
            
    except MpesaPayment.DoesNotExist:
        logger.error(f"Payment record not found for: {checkout_request_id}")
        return JsonResponse({
            'status': 'error',
            'message': 'Payment record not found'
        })
    except Exception as e:
        logger.error(f"Error checking payment status: {str(e)}", exc_info=True)
        return JsonResponse({
            'status': 'error',
            'message': 'Error checking payment status'
        })

@login_required
def remove_from_cart(request, item_id):
    if request.method == 'POST':
        # Get the user's cart
        cart, _ = Cart.objects.get_or_create(user=request.user)
        
        try:
            # Get the cart item using the cart relationship
            cart_item = CartItem.objects.get(id=item_id, cart=cart)
            cart_item.delete()
            messages.success(request, 'Item removed from cart successfully.')
        except CartItem.DoesNotExist:
            messages.error(request, 'Item not found in cart.')
            
    return redirect('shopping_cart:cart_detail')

@login_required
def toggle_wishlist(request, product_id):
    if request.method == 'POST':
        product = get_object_or_404(Product, id=product_id)
        wishlist, created = Wishlist.objects.get_or_create(user=request.user)
        
        if product in wishlist.products.all():
            wishlist.products.remove(product)
            messages.success(request, f"{product.name} removed from your wishlist.")
        else:
            wishlist.products.add(product)
            messages.success(request, f"{product.name} added to your wishlist.")
            
    return redirect('shopease:product_detail', product_id=product_id)

@login_required
def wishlist(request):
    wishlist, created = Wishlist.objects.get_or_create(user=request.user)
    context = {
        'wishlist_items': wishlist.products.all()
    }
    return render(request, 'wishlist.html', context)

@login_required
def cart_detail(request):
    cart_items = CartItem.objects.filter(user=request.user)
    total = sum(item.total_price for item in cart_items)
    
    context = {
        'cart_items': cart_items,
        'total': total
    }
    return render(request, 'shopease/cart.html', context)

@login_required
def my_orders(request):
    orders = Order.objects.filter(user=request.user).order_by('-created_at')
    
    for order in orders:
        # Get the latest tracking status
        order.current_status = order.tracking_updates.first()
        # Get installment info if exists
        try:
            order.installment_info = order.installment_plan
        except InstallmentPlan.DoesNotExist:
            order.installment_info = None
    
    context = {
        'orders': orders,
    }
    return render(request, 'shopease/my_orders.html', context)

@login_required
def order_detail(request, order_id):
    order = get_object_or_404(Order, id=order_id, user=request.user)
    tracking_updates = order.tracking_updates.all()
    
    try:
        installment_plan = order.installment_plan
        installment_payments = installment_plan.payments.all()
    except InstallmentPlan.DoesNotExist:
        installment_plan = None
        installment_payments = None
    
    context = {
        'order': order,
        'tracking_updates': tracking_updates,
        'installment_plan': installment_plan,
        'installment_payments': installment_payments,
    }
    return render(request, 'shopease/order_detail.html', context)

@login_required
def make_installment_payment(request, plan_id):
    if request.method == 'POST':
        plan = get_object_or_404(InstallmentPlan, id=plan_id, user=request.user)
        amount = Decimal(request.POST.get('amount'))
        
        if amount <= 0:
            messages.error(request, 'Payment amount must be greater than zero.')
            return redirect('shopease:order_detail', order_id=plan.order.id)
        
        # Create payment record
        payment = InstallmentPayment.objects.create(
            installment_plan=plan,
            amount=amount,
            payment_method=request.POST.get('payment_method'),
            notes=request.POST.get('notes', '')
        )
        
        # Update plan amount paid
        plan.amount_paid += amount
        if plan.amount_paid >= plan.total_amount:
            plan.is_completed = True
        plan.last_payment_date = timezone.now()
        plan.save()
        
        messages.success(request, f'Payment of ${amount} recorded successfully.')
        return redirect('shopease:order_detail', order_id=plan.order.id)
    
    return redirect('shopease:my_orders')

def contact(request):
    return render(request, 'contact.html')

def help(request):
    return render(request, 'help.html')

def terms_conditions(request):
    return render(request, 'terms_conditions.html')

def privacy_policy(request):
    return render(request, 'privacy_policy.html')

def about_us(request):
    return render(request, 'about_us.html')

@require_POST
def contact_submit(request):
    try:
        # Get form data
        name = request.POST.get('name')
        email = request.POST.get('email')
        subject = request.POST.get('subject')
        message = request.POST.get('message')
        
        # Validate required fields
        if not all([name, email, subject, message]):
            return JsonResponse({
                'success': False,
                'message': 'Please fill in all fields.'
            })
        
        # Save to database
        Contact.objects.create(
            name=name,
            email=email,
            subject=subject,
            message=message
        )
        
        return JsonResponse({
            'success': True,
            'message': 'Thank you for your message. We will get back to you soon!'
        })
        
    except Exception as e:
        print(f"Error in contact_submit: {str(e)}")  # For debugging
        return JsonResponse({
            'success': False,
            'message': 'An error occurred. Please try again.'
        })

@require_POST
def newsletter_signup(request):
    try:
        email = request.POST.get('email')
        
        if not email:
            return JsonResponse({
                'success': False,
                'message': 'Please provide an email address.'
            })
        
        # Check if email already exists
        if NewsletterSubscriber.objects.filter(email=email).exists():
            return JsonResponse({
                'success': False,
                'message': 'This email is already subscribed.'
            })
            
        # Create new subscriber
        NewsletterSubscriber.objects.create(email=email)
        return JsonResponse({
            'success': True,
            'message': 'Thank you for subscribing to our newsletter!'
        })
        
    except Exception as e:
        print(f"Error in newsletter_signup: {str(e)}")  # For debugging
        return JsonResponse({
            'success': False,
            'message': 'An error occurred. Please try again.'
        })

@login_required
@require_POST
def update_cart_item(request, item_id):
    if request.method == 'POST':
        try:
            cart_item = CartItem.objects.get(id=item_id, cart__user=request.user)
            action = request.POST.get('action')
            
            if action == 'increase':
                cart_item.quantity += 1
            elif action == 'decrease' and cart_item.quantity > 1:
                cart_item.quantity -= 1
                
            cart_item.save()
            messages.success(request, 'Cart updated successfully')
            
        except CartItem.DoesNotExist:
            messages.error(request, 'Item not found in cart')
            
    return redirect('shopease:view_cart')

@login_required
@require_POST
def remove_cart_item(request, item_id):
    if request.method == 'POST':
        try:
            cart_item = CartItem.objects.get(id=item_id, cart__user=request.user)
            cart_item.delete()
            messages.success(request, 'Item removed from cart successfully.')
        except CartItem.DoesNotExist:
            messages.error(request, 'Item not found in your cart.')
    
    return redirect('shopping_cart:cart_detail')

@login_required
def cart_view(request):
    cart, created = Cart.objects.get_or_create(user=request.user)
    cart_items = CartItem.objects.filter(cart=cart)
    
    context = {
        'cart_items': cart_items,
    }
    return render(request, 'shopease/cart.html', context)

@login_required
@require_POST
def update_cart(request, item_id):
    try:
        data = json.loads(request.body)
        quantity = int(data.get('quantity', 1))
        
        cart_item = get_object_or_404(CartItem, id=item_id, cart__user=request.user)
        cart_item.quantity = quantity
        cart_item.save()
        
        # Calculate new cart total
        cart = cart_item.cart
        total = sum(item.product.price * item.quantity for item in cart.items.all())
        
        return JsonResponse({
            'success': True,
            'cart_total': float(total)
        })
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({
            'success': False,
            'message': 'Invalid quantity'
        }, status=400)
    except CartItem.DoesNotExist:
        return JsonResponse({
            'success': False,
            'message': 'Item not found'
        }, status=404)

@login_required
@require_POST
def remove_from_cart(request, item_id):
    try:
        cart_item = get_object_or_404(CartItem, id=item_id, cart__user=request.user)
        cart = cart_item.cart
        cart_item.delete()
        
        # Calculate new cart total using CartItem.objects.filter instead of cart.items.all()
        total = sum(item.product.price * item.quantity for item in CartItem.objects.filter(cart=cart))
        
        return JsonResponse({
            'success': True,
            'cart_total': float(total)
        })
    except CartItem.DoesNotExist:
        return JsonResponse({
            'success': False,
            'message': 'Item not found'
        }, status=404)

@login_required
@require_POST
def initiate_bnpl(request):
    try:
        # Get the current cart total
        cart = request.user.cart
        total_amount = cart.total_amount
        
        # Create the order
        order = Order.objects.create(
            user=request.user,
            total_amount=total_amount,
            payment_method='BNPL'
        )
        
        # Calculate installment amount (divide by 3 for three monthly payments)
        installment_amount = Decimal(total_amount) / 3
        
        # Create three monthly installments
        for i in range(3):
            due_date = datetime.now().date() + timedelta(days=30 * (i + 1))
            BNPLInstallment.objects.create(
                user=request.user,
                order=order,
                amount=installment_amount,
                due_date=due_date
            )
        
        # Clear the cart
        cart.clear()
        
        return JsonResponse({
            'status': 'success',
            'message': 'BNPL payment plan created successfully',
            'order_id': order.id
        })
        
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'message': str(e)
        }, status=400)

@login_required
def my_installments(request):
    installments = BNPLInstallment.objects.filter(user=request.user)
    context = {
        'installments': installments
    }
    return render(request, 'shopease/my_installments.html', context)

@login_required
def select_installment_plan(request):
    try:
        cart = Cart.objects.get(user=request.user)
        cart_items = CartItem.objects.filter(cart=cart).select_related('product')
        
        if not cart_items.exists():
            messages.warning(request, "Your cart is empty.")
            return redirect('shopease:cart_detail')
        
        total = sum(item.get_total() for item in cart_items)
        
        # Calculate monthly payments for different plans
        installment_options = [
            {
                'months': 3,
                'monthly_payment': total / 3,
                'total_amount': total
            },
            {
                'months': 6,
                'monthly_payment': total / 6,
                'total_amount': total
            },
            {
                'months': 12,
                'monthly_payment': total / 12,
                'total_amount': total
            }
        ]
        
        context = {
            'cart_items': cart_items,
            'total': total,
            'installment_options': installment_options,
        }
        
        return render(request, 'select_installment_plan.html', context)
        
    except Cart.DoesNotExist:
        messages.error(request, "No active cart found.")
        return redirect('shopease:cart_detail')

@login_required
def process_installment_plan(request):
    if request.method == 'POST':
        try:
            # Get cart total
            cart = Cart.objects.get(user=request.user)
            cart_items = CartItem.objects.filter(cart=cart)
            total_amount = sum(item.quantity * item.product.price for item in cart_items)
            
            # Get and validate installment details
            months = int(request.POST.get('months', 0))
            if months not in [3, 6, 12]:  # Only allow valid installment periods
                raise ValueError('Invalid installment period')
                
            # Calculate monthly payment
            monthly_payment = total_amount / months
            
            # Store the installment plan details in the session
            request.session['is_installment'] = True
            request.session['installment_number'] = 1
            request.session['total_installments'] = months
            request.session['monthly_payment'] = str(monthly_payment)
            request.session['total_amount'] = str(total_amount)
            request.session['installment_months'] = months
            
            # Redirect to payment form where user can enter phone number
            return redirect('shopease:payment')
            
        except (Cart.DoesNotExist, ValueError, TypeError) as e:
            messages.error(request, str(e) if str(e) != '' else 'Invalid installment plan')
            return redirect('shopease:select_installment_plan')
        except Exception as e:
            logger.error(f"Error processing installment plan: {str(e)}")
            messages.error(request, 'An error occurred while processing your request')
            return redirect('shopease:select_installment_plan')

    # Handle GET request
    return render(request, 'select_installment_plan.html')

@login_required
def process_payment(request):
    try:
        # Check if this is an installment payment
        is_installment = request.session.get('is_installment', False)
        
        if is_installment:
            try:
                # Get monthly payment amount from session and convert to float
                monthly_payment = float(request.session.get('monthly_payment', 0))
                if monthly_payment <= 0:
                    raise ValueError('Invalid monthly payment amount')
                amount_to_pay = monthly_payment
            except (ValueError, TypeError):
                messages.error(request, 'Invalid monthly payment amount')
                return redirect('shopease:payment')
        else:
            # For full payment, get cart total
            cart = Cart.objects.get(user=request.user)
            cart_items = CartItem.objects.filter(cart=cart)
            amount_to_pay = float(sum(item.quantity * item.product.price for item in cart_items))
            
        # Get phone number from form
        phone_number = request.POST.get('phone_number')
        if not phone_number:
            messages.error(request, 'Phone number is required')
            return redirect('shopease:payment')
        
        # Format phone number if needed
        if phone_number.startswith('0'):
            phone_number = '254' + phone_number[1:]
        elif not phone_number.startswith('254'):
            phone_number = '254' + phone_number
            
        # Initialize M-PESA client and send payment request
        mpesa_client = MpesaClient()
        response = mpesa_client.stk_push(
            phone_number=phone_number,
            amount=int(amount_to_pay),
            reference=f"ORDER-{request.user.id}-{timezone.now().strftime('%Y%m%d%H%M%S')}"
        )
        
        if response.get('success'):
            if not is_installment:
                # For full payment, redirect directly to thank you page
                return redirect('shopease:thank_you')
            else:
                # For installments, show payment status
                request.session['amount'] = str(amount_to_pay)
                request.session['mpesa_transaction_id'] = response.get('checkout_request_id')
                return redirect('shopease:payment_status')
        else:
            messages.error(request, response.get('message', 'Failed to initiate payment'))
            return redirect('shopease:payment')
            
    except Exception as e:
        logger.error(f"Error processing payment: {str(e)}", exc_info=True)
        messages.error(request, "An error occurred while processing your payment.")
        return redirect('shopease:payment')

@login_required
def add_to_cart_hot_deal(request, product_id):
    if request.method == 'POST':
        try:
            product = Product.objects.get(id=product_id)
            cart, created = Cart.objects.get_or_create(user=request.user)
            cart_item, item_created = CartItem.objects.get_or_create(cart=cart, product=product)
            
            if not item_created:
                cart_item.quantity += 1
                cart_item.save()
            
            messages.success(request, f"{product.name} has been added to your cart.")
            return redirect('shopping_cart:cart_detail')
            
        except Product.DoesNotExist:
            messages.error(request, "Product not found.")
            return redirect('shopping_cart:cart_detail')
        except Exception as e:
            messages.error(request, f"Error adding product to cart: {str(e)}")
    
    return redirect('shopease:hot_deals')

def random_reference():
    """Generate a random reference number for orders"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def verify_payment(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request method'})
    
    checkout_request_id = request.session.get('checkout_request_id')
    if not checkout_request_id:
        return JsonResponse({
            'status': 'error',
            'message': 'No payment verification pending'
        })
    
    result = verify_transaction(checkout_request_id)
    
    if result['success']:
        # Clear the session data
        del request.session['checkout_request_id']
        if 'payment_reference' in request.session:
            del request.session['payment_reference']
            
        return JsonResponse({
            'status': 'success',
            'message': result['message']
        })
    else:
        return JsonResponse({
            'status': 'error',
            'message': result['message']
        })

def initiate_payment(request):
    try:
        # Log the start of payment process
        logger.info("Starting M-PESA payment process")
        
        # Log the request data
        logger.info(f"Payment details: {request.POST}")
        
        # Your payment processing code here
        response = mpesa_utils.stk_push(...)
        
        # Log the M-PESA response
        logger.info(f"M-PESA API Response: {response}")
        
        return response
        
    except Exception as e:
        # Log any errors
        logger.error(f"Payment failed: {str(e)}", exc_info=True)
        return JsonResponse({'error': 'Payment failed'}, status=400)
    
@login_required
def check_payment_status(request, checkout_request_id):
    try:
        logger.info(f"Checking payment status for: {checkout_request_id}")
        
        # Get the payment record
        payment = MpesaPayment.objects.get(transaction_id=checkout_request_id)
        
        # Query M-PESA for status
        mpesa_client = MpesaClient()
        result = mpesa_client.query_payment_status(checkout_request_id)
        logger.info(f"Payment status result: {result}")
        
        if result.get('success'):
            # Update payment status
            payment.status = 'completed'
            payment.save()
            
            # Create order
            order = Order.objects.create(
                user=request.user,
                total_amount=payment.amount,
                payment_method='M-PESA',
                payment_status='completed',
                mpesa_payment=payment
            )
            
            # Move cart items to order items
            cart = Cart.objects.get(user=request.user)
            cart_items = CartItem.objects.filter(cart=cart)
            
            for cart_item in cart_items:
                OrderItem.objects.create(
                    order=order,
                    product=cart_item.product,
                    quantity=cart_item.quantity,
                    price=cart_item.product.price
                )
            
            # Clear the cart
            cart_items.delete()
            
            messages.success(request, 'Payment completed successfully!')
            
            # Check if this is a full payment or installment
            if not request.session.get('is_installment'):
                return JsonResponse({
                    'status': 'completed',
                    'redirect_url': reverse('shopease:thank_you')
                })
            else:
                return JsonResponse({
                    'status': 'completed',
                    'redirect_url': reverse('shopease:order_confirmation', kwargs={'order_id': order.id})
                })
        else:
            # Update payment status if failed
            payment.status = 'failed'
            payment.save()
            
            return JsonResponse({
                'status': 'error',
                'message': 'Payment failed. Please try again.'
            })
            
    except MpesaPayment.DoesNotExist:
        logger.error(f"Payment record not found for: {checkout_request_id}")
        return JsonResponse({
            'status': 'error',
            'message': 'Payment record not found'
        })
    except Exception as e:
        logger.error(f"Error checking payment status: {str(e)}", exc_info=True)
        return JsonResponse({
            'status': 'error',
            'message': 'Error checking payment status'
        })

def payment_status(request):
    if request.session.get('is_installment'):
        try:
            # Get the stored payment amount and total amount
            monthly_payment = float(request.session.get('monthly_payment', 0))
            total_amount = float(request.session.get('total_amount', 0))
            
            if monthly_payment <= 0:
                messages.error(request, 'Invalid payment amount')
                return redirect('shopease:payment')
                
            context = {
                'installment_number': request.session.get('installment_number'),
                'total_installments': request.session.get('total_installments'),
                'amount': monthly_payment,
                'total_amount': total_amount
            }
            
            # Log the amounts being displayed
            logger.info(f"Payment status - Monthly: {monthly_payment}, Total: {total_amount}")
            
            return render(request, 'payment_status.html', context)
        except (ValueError, TypeError) as e:
            logger.error(f"Error in payment status: {str(e)}")
            messages.error(request, 'Invalid payment amount')
            return redirect('shopease:payment')
    else:
        try:
            amount = float(request.session.get('amount', 0))
            if amount <= 0:
                messages.error(request, 'Invalid payment amount')
                return redirect('shopease:checkout')
                
            return render(request, 'payment_status_full.html', {
                'amount': amount
            })
        except (ValueError, TypeError):
            messages.error(request, 'Invalid payment amount')
            return redirect('shopease:checkout')

@login_required
def payment_processing(request):
    cart = Cart.objects.get(user=request.user)
    cart_items = CartItem.objects.filter(cart=cart)
    total_amount = sum(item.quantity * item.product.price for item in cart_items)
    
    months = request.session.get('installment_months', 6)
    monthly_payment = total_amount / months
    
    # Store in session
    request.session['monthly_payment'] = str(monthly_payment)
    request.session['total_amount'] = str(total_amount)
    
    context = {
        'cart_items': cart_items,
        'total_amount': total_amount,
        'months': months,
        'monthly_payment': monthly_payment
    }
    
    return render(request, 'payment_processing.html', context)

def verify_transaction(checkout_request_id):
    try:
        mpesa_client = MpesaClient()
        result = mpesa_client.query_payment_status(checkout_request_id)
        return {
            'success': result['success'],
            'message': result.get('message', 'Payment status checked')
        }
    except Exception as e:
        return {
            'success': False,
            'message': str(e)
        }

@csrf_exempt
def mpesa_callback(request):
    """Handle M-PESA payment callbacks from Safaricom"""
    if request.method == 'POST':
        try:
            # Parse the callback data
            callback_data = json.loads(request.body)
            logger.info(f"M-PESA Callback received: {callback_data}")
            
            # Extract relevant information
            result_code = callback_data.get('ResultCode')
            checkout_request_id = callback_data.get('CheckoutRequestID')
            
            try:
                # Get the payment record
                payment = MpesaPayment.objects.get(transaction_id=checkout_request_id)
                
                if result_code == '0':  # Success
                    # Update payment status
                    payment.status = 'completed'
                    payment.save()
                    
                    # Create order if it doesn't exist
                    if not Order.objects.filter(mpesa_payment=payment).exists():
                        order = Order.objects.create(
                            user=payment.user,
                            total_amount=payment.amount,
                            payment_method='M-PESA',
                            payment_status='completed',
                            mpesa_payment=payment
                        )
                        
                        # Move cart items to order items
                        cart = Cart.objects.get(user=payment.user)
                        cart_items = CartItem.objects.filter(cart=cart)
                        
                        for cart_item in cart_items:
                            OrderItem.objects.create(
                                order=order,
                                product=cart_item.product,
                                quantity=cart_item.quantity,
                                price=cart_item.product.price
                            )
                        
                        # Clear the cart
                        cart_items.delete()
                        
                else:  # Failed
                    payment.status = 'failed'
                    payment.save()
                
                return JsonResponse({
                    'ResultCode': 0,
                    'ResultDesc': 'Callback processed successfully'
                })
                
            except MpesaPayment.DoesNotExist:
                logger.error(f"Payment record not found for checkout request ID: {checkout_request_id}")
                return JsonResponse({
                    'ResultCode': 1,
                    'ResultDesc': 'Payment record not found'
                })
                
        except json.JSONDecodeError:
            logger.error("Invalid JSON in callback request")
            return JsonResponse({
                'ResultCode': 1,
                'ResultDesc': 'Invalid JSON data'
            })
        except Exception as e:
            logger.error(f"Error processing M-PESA callback: {str(e)}", exc_info=True)
            return JsonResponse({
                'ResultCode': 1,
                'ResultDesc': 'Internal server error'
            })
    
    return JsonResponse({
        'ResultCode': 1,
        'ResultDesc': 'Invalid request method'
    })

@login_required
def payment(request):
    monthly_payment = request.session.get('monthly_payment')
    installment_number = request.session.get('installment_number')
    total_installments = request.session.get('total_installments')
    
    context = {
        'monthly_payment': monthly_payment,
        'installment_number': installment_number,
        'total_installments': total_installments,
    }
    
    return render(request, 'payment.html', context)

@login_required
def clear_cart(request):
    """Clear all items from the user's cart"""
    try:
        cart = Cart.objects.get(user=request.user)
        cart.items.all().delete()  # Delete all cart items
        messages.success(request, 'Your cart has been cleared.')
    except Cart.DoesNotExist:
        messages.info(request, 'Your cart is already empty.')
    
    return redirect('shopping_cart:cart_detail')