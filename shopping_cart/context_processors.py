from .models import Cart, CartItem

def cart(request):
    if request.user.is_authenticated:
        cart, created = Cart.objects.get_or_create(user=request.user)
        cart_items = CartItem.objects.filter(cart=cart).select_related('product')
        total = sum(item.quantity * item.product.price for item in cart_items)
        return {
            'cart_items': cart_items,
            'total': total,
        }
    return {
        'cart_items': [],
        'total': 0,
    } 