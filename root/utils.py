from .models import Business

def get_active_business(request):

    if request.user and request.user.is_authenticated:
        return Business.objects.get(owner_id = request.user.id, is_active=True)
    
    return None